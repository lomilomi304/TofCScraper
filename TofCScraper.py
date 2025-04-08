import argparse
import csv
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, quote, urlencode
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

stats = {
    # Stage 1 stats
    'total_records': 0,
    'records_with_isbn': 0,
    'records_with_lccn': 0,
    'records_with_both': 0,
    'records_with_none': 0,
    'errors_stage1': 0,
    
    # Stage 2 stats
    'items_requiring_lookup': 0,
    'successful_isbn_lookups': 0,
    'successful_title_lookups': 0,
    'failed_lookups': 0,
    
    # Stage 3 stats
    'total_505_searches': 0,
    'found_505': 0,
    'empty_505': 0,
    'missing_505': 0,
    'errors_stage3': 0
}

class CatalogProcessor:
    
    def __init__(self, args):
        self.args = args
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        })
        
        self.temp_dir = os.path.join(os.path.dirname(args.output), "temp")
        os.makedirs(self.temp_dir, exist_ok=True)
        
        filename = os.path.splitext(os.path.basename(args.output))[0]
        self.stage1_output = os.path.join(self.temp_dir, f"{filename}_stage1.csv")
        self.stage2_output = os.path.join(self.temp_dir, f"{filename}_stage2.csv")
        
    def run(self):
        try:
            if not os.path.exists(self.args.input):
                print(f"Error: Input file '{self.args.input}' not found.")
                return 1
            
            # Stage 1
            if not self.args.skip_stage1:
                print("\n===== STAGE 1: Extracting ISBNs/LCCNs from catalog =====")
                success = self.run_stage1()
                if not success:
                    print("Stage 1 failed. Cannot proceed to Stage 2.")
                    return 1
                stage2_input = self.stage1_output
            else:
                print("\n===== STAGE 1: Skipped =====")
                stage2_input = self.args.input
            
            # Stage 2
            if not self.args.skip_stage2:
                print("\n===== STAGE 2: Looking up missing LCCNs from LC =====")
                success = self.run_stage2(stage2_input)
                if not success:
                    print("Stage 2 failed. Cannot proceed to Stage 3.")
                    return 1
                stage3_input = self.stage2_output
            else:
                print("\n===== STAGE 2: Skipped =====")
                stage3_input = stage2_input if not self.args.skip_stage1 else self.args.input
            
            # Stage 3
            if not self.args.skip_stage3:
                print("\n===== STAGE 3: Retrieving 505 fields from LC =====")
                success = self.run_stage3(stage3_input)
                if not success:
                    print("Stage 3 failed.")
                    return 1
            else:
                print("\n===== STAGE 3: Skipped =====")
            
            self.print_summary()
            
            if self.args.clean_temp:
                print("\nCleaning up temporary files...")
                if os.path.exists(self.stage1_output):
                    os.remove(self.stage1_output)
                if os.path.exists(self.stage2_output):
                    os.remove(self.stage2_output)
                try:
                    os.rmdir(self.temp_dir)
                except OSError:
                    pass
            
            print(f"\nProcessing complete! Final results saved to: {self.args.output}")
            return 0
            
        except Exception as e:
            print(f"Unexpected error: {e}")
            import traceback
            traceback.print_exc()
            return 1

    # STAGE 1: OPAC scrape
    
    def run_stage1(self):
        try:
            print(f"Parsing input CSV file: {self.args.input}")
            records, error = self.parse_csv(self.args.input)
            
            if error:
                print(f"Error parsing CSV: {error}")
                return False
            
            print(f"Found {len(records)} records to process.")
            stats['total_records'] = len(records)
            
            print(f"Processing records with {self.args.delay}s delay between requests...")
            results, _ = self.process_catalog_records(records, self.args.delay)
            
            error = self.save_stage1_results(results, self.stage1_output)
            if error:
                print(error)
                return False
            
            print(f"Stage 1 results saved to {self.stage1_output}")
            return True
            
        except Exception as e:
            print(f"Error in Stage 1: {e}")
            stats['errors_stage1'] += 1
            return False
    
    def parse_csv(self, file_path):
        records = []
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                reader = csv.reader(file)
                headers = next(reader, None)
                
                # find the index of the bibid and title columns
                bibid_col = None
                title_col = None
                
                for i, header in enumerate(headers):
                    if header and "BibID" in header:
                        bibid_col = i
                    elif header and "title" in header.lower():
                        title_col = i
                
                if bibid_col is None or title_col is None:
                    return [], "Could not find BibID and/or title columns in the CSV file."
                
                for row in reader:
                    if len(row) > max(bibid_col, title_col):
                        bibid = row[bibid_col].strip()
                        title = row[title_col].strip()
                        
                        if bibid:
                            bibid_match = re.search(r'(\d+)', bibid)
                            if bibid_match:
                                bibid = bibid_match.group(1)
                            
                            records.append({
                                'bibid': bibid,
                                'title': title
                            })
            
            if not records:
                return [], "No valid records found in the CSV file."
            
            return records, None
        except Exception as e:
            return [], f"Error parsing CSV: {str(e)}"

    def scrape_catalog_record(self, bibid):
        url = f"https://islandpines.roblib.upei.ca/eg/opac/record/{bibid}?expand=marchtml#marchtml"
        
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            isbns = []
            lccns = []
            
            # Look specifically for each tag by number in the marc_tag_col elements
            tag_cols = soup.find_all('th', class_='marc_tag_col')
            
            for tag_col in tag_cols:
                tag_text = tag_col.get_text().strip()
                
                # Find the parent row and then the subfields column
                if tag_text == '020':  # ISBN
                    row = tag_col.parent
                    subfields_td = row.find('td', class_='marc_subfields')
                    if subfields_td:
                        # find subfield 'a'
                        for span in subfields_td.find_all('span'):
                            span_text = span.get_text().strip()
                            if span_text.endswith('a'):
                                value = span.next_sibling.strip()
                                # Clean ISBN
                                isbn = re.sub(r'[^\dX]', '', value)
                                if isbn:
                                    isbns.append(isbn)
                
                elif tag_text == '010':  # LCCN
                    row = tag_col.parent
                    subfields_td = row.find('td', class_='marc_subfields')
                    if subfields_td:
                        # find subfield 'a'
                        for span in subfields_td.find_all('span'):
                            span_text = span.get_text().strip()
                            if span_text.endswith('a'):
                                value = span.next_sibling.strip()
                                # Split to get the first part (before any '/')
                                lccn = value.split()[0].strip()
                                if lccn:
                                    lccns.append(lccn)
            
            return {
                'isbns': isbns,
                'lccns': lccns
            }, None
        
        except Exception as e:
            if self.args.verbose:
                import traceback
                print(f"Error scraping {bibid}: {str(e)}")
                print(traceback.format_exc())
            return {
                'isbns': [],
                'lccns': []
            }, str(e)

    def process_catalog_records(self, records, delay=1):
        results = []
        
        pbar = tqdm(total=len(records), desc="Scraping catalog records", unit="record")
        
        for record in records:
            time.sleep(delay)
            
            scraped_data, error = self.scrape_catalog_record(record['bibid'])
            
            result = {
                'bibid': record['bibid'],
                'title': record['title'],
                'isbns': scraped_data['isbns'],
                'lccns': scraped_data['lccns'],
                'error': error
            }
            
            # Update stats
            has_isbn = len(scraped_data['isbns']) > 0
            has_lccn = len(scraped_data['lccns']) > 0
            
            if error:
                stats['errors_stage1'] += 1
            elif has_isbn and has_lccn:
                stats['records_with_both'] += 1
                stats['records_with_isbn'] += 1
                stats['records_with_lccn'] += 1
            elif has_isbn:
                stats['records_with_isbn'] += 1
            elif has_lccn:
                stats['records_with_lccn'] += 1
            else:
                stats['records_with_none'] += 1
            
            results.append(result)
            pbar.update(1)
        
        pbar.close()
        return results, stats

    def save_stage1_results(self, results, output_file):
        """Save results from Stage 1 to a CSV file."""
        try:
            with open(output_file, 'w', newline='', encoding='utf-8') as file:
                writer = csv.writer(file)
                
                writer.writerow(['BibID', 'Title', 'ISBN', 'LCCN', 'Error'])
                
                for record in results:
                    isbns_str = '; '.join(record['isbns']) if record['isbns'] else ''
                    lccns_str = '; '.join(record['lccns']) if record['lccns'] else ''
                    error = record['error'] if 'error' in record and record['error'] else ''
                    
                    writer.writerow([
                        record['bibid'],
                        record['title'],
                        isbns_str,
                        lccns_str,
                        error
                    ])
            return None
        except Exception as e:
            return f"Error saving results: {str(e)}"

    # STAGE 2: LCCN search
    
    def run_stage2(self, input_file):
        try:

            if not os.path.isfile(input_file):
                print(f"Error: Input file '{input_file}' not found.")
                return False
                
            with open(input_file, 'r', newline='', encoding='utf-8') as csv_in:
                reader = csv.DictReader(csv_in)
                
                # check if required columns exist
                required_columns = ['Title', 'ISBN', 'LCCN']
                for col in required_columns:
                    if col not in reader.fieldnames:
                        print(f"Error: Required column '{col}' not found in input CSV.")
                        return False
                
                records = list(reader)
                
            output_records = []
            
            for i, record in enumerate(records):
                bibid = record.get('BibID', '')
                title = record.get('Title', '')
                isbn = record.get('ISBN', '')
                lccn = record.get('LCCN', '')
                
                if not title:
                    continue
                    
                # If LCCN already exists, keep it
                if lccn and lccn.strip():
                    output_records.append({
                        'BibID': bibid,
                        'Title': title,
                        'ISBN': isbn,
                        'LCCN': lccn.strip()
                    })
                    print(f"[{i+1}/{len(records)}] Item already has LCCN: {lccn}")
                    continue
                    
                # If no ISBN, skip this lookup but keep the record
                if not isbn or not isbn.strip():
                    output_records.append({
                        'BibID': bibid,
                        'Title': title,
                        'ISBN': isbn,
                        'LCCN': ''
                    })
                    print(f"[{i+1}/{len(records)}] Item has no ISBN, skipping lookup: {title}")
                    continue
                    
                # Need to look up LCCN
                stats['items_requiring_lookup'] += 1
                print(f"[{i+1}/{len(records)}] Looking up LCCN for: {title}")
                
                # First try with ISBN
                print(f"  Searching by ISBN: {isbn}")
                found_lccn = self.scrape_lccn_by_isbn(isbn)
                
                # If ISBN search fails, try with title
                if not found_lccn:
                    print(f"  ISBN search failed, trying title search...")
                    time.sleep(self.args.delay) 
                    found_lccn = self.scrape_lccn_by_title(title)
                    if found_lccn:
                        stats['successful_title_lookups'] += 1
                else:
                    stats['successful_isbn_lookups'] += 1
                
                time.sleep(self.args.delay)
                
                if found_lccn:
                    print(f"✓ Found LCCN: {found_lccn}")
                    output_records.append({
                        'BibID': bibid,
                        'Title': title,
                        'ISBN': isbn,
                        'LCCN': found_lccn
                    })
                else:
                    stats['failed_lookups'] += 1
                    print(f"✗ LCCN not found for: {title}")
                    # Still include in output but with empty LCCN
                    output_records.append({
                        'BibID': bibid,
                        'Title': title,
                        'ISBN': isbn,
                        'LCCN': ''
                    })
            
            # Write output
            with open(self.stage2_output, 'w', newline='', encoding='utf-8') as csv_out:
                fieldnames = ['BibID', 'Title', 'ISBN', 'LCCN']
                writer = csv.DictWriter(csv_out, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(output_records)
                
            print(f"Stage 2 results saved to {self.stage2_output}")
            return True
            
        except Exception as e:
            print(f"Error in Stage 2: {e}")
            import traceback
            traceback.print_exc()
            return False

    def extract_lccn_from_html(self, html_content):
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # try 1: Look for LCCN in the specific wrapper div
        lccn_wrappers = soup.find_all('div', class_='items-wrapper')
        for wrapper in lccn_wrappers:
            header = wrapper.find('h3', class_='item-title')
            if header and header.text.strip() == 'LCCN':
                item_desc = wrapper.find('ul', class_='item-description')
                if item_desc:
                    lccn_elem = item_desc.find('span', dir='ltr')
                    if lccn_elem:
                        lccn = lccn_elem.text.strip()
                        return lccn
        
        # try 2: Look for LCCN permalink
        permalink_wrappers = soup.find_all('div', class_='items-wrapper')
        for wrapper in permalink_wrappers:
            header = wrapper.find('h3', class_='item-title')
            if header and 'LCCN Permalink' in header.text:
                item_desc = wrapper.find('ul', class_='item-description')
                if item_desc:
                    permalink = item_desc.find('a', id='permalink')
                    if permalink and 'href' in permalink.attrs:
                        lccn_match = re.search(r'lccn\.loc\.gov/(\d+)', permalink['href'])
                        if lccn_match:
                            return lccn_match.group(1)
        
        # try 3: Look for LCCN in the Z3988
        z3988_span = soup.find('span', class_='Z3988')
        if z3988_span and 'title' in z3988_span.attrs:
            lccn_match = re.search(r'rft\.lccn=(\d+)', z3988_span['title'])
            if lccn_match:
                return lccn_match.group(1)
        
        # try 4: Search entire page for any element containing LCCN
        content_container = soup.find('div', class_='content-container')
        if content_container:
            for div in content_container.find_all('div'):
                if 'LCCN' in div.text:
                    # Look for text that contains digits, likely to be the LCCN
                    lccn_text = re.search(r'\b\d{8,}\b', div.text)
                    if lccn_text:
                        return lccn_text.group(0).strip()
        
        return None

    def scrape_lccn_by_isbn(self, isbn):
        clean_isbn = re.sub(r'[^0-9X]', '', isbn)
        if not clean_isbn:
            return None
            
        # Base URL and search parameters
        base_url = "https://catalog.loc.gov/vwebv/search"
        params = {
            "searchArg1": clean_isbn,
            "argType1": "all",
            "searchCode1": "KNUM",
            "searchType": "2",
            "combine2": "and"
        }
        
        for attempt in range(self.args.max_retries):
            try:
                response = self.session.get(base_url, params=params, timeout=30)
                response.raise_for_status()
                
                return self.extract_lccn_from_html(response.text)
                
            except requests.RequestException as e:
                if self.args.verbose:
                    print(f"Request error (attempt {attempt+1}/{self.args.max_retries}): {e}")
                if attempt < self.args.max_retries - 1:
                    time.sleep(self.args.delay * (attempt + 1))  # Exponential backoff
            
            except Exception as e:
                if self.args.verbose:
                    print(f"Error processing ISBN {isbn}: {e}")
                return None
                
        return None

    def scrape_lccn_by_title(self, title):
        if not title or not title.strip():
            return None
            
        clean_title = title.strip()
        
        base_url = "https://catalog.loc.gov/vwebv/search"
        params = {
            "searchArg": clean_title,
            "searchCode": "GKEY^*",
            "searchType": "0",
            "recCount": "25"
        }
        
        try:
            if self.args.verbose:
                print(f"Searching by title: {clean_title}")
            response = self.session.get(base_url, params=params, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            result_table = soup.find('table', class_='browseList')
            if result_table:
                first_result = result_table.find('a', class_='browse-result')
                if first_result and 'href' in first_result.attrs:
                    detail_url = "https://catalog.loc.gov" + first_result['href']
                    if self.args.verbose:
                        print(f"Found first result, fetching details: {detail_url}")
                    
                    time.sleep(self.args.delay)
                    
                    detail_response = self.session.get(detail_url, timeout=30)
                    detail_response.raise_for_status()
                    
                    return self.extract_lccn_from_html(detail_response.text)
            else:
                return self.extract_lccn_from_html(response.text)
                
        except requests.RequestException as e:
            if self.args.verbose:
                print(f"Title search request error: {e}")
        
        except Exception as e:
            if self.args.verbose:
                print(f"Error processing title search for '{title}': {e}")
            
        return None

    # STAGE 3: 505s
    
    def run_stage3(self, input_file):
        try:
            entries = self.read_lccn_file(input_file)
            if not entries:
                print("No valid entries found for Stage 3.")
                return False
                
            stats['total_505_searches'] = len(entries)
            print(f"Found {len(entries)} entries to process for 505 field retrieval")
            
            self.process_505_entries(entries)
            
            return True
            
        except Exception as e:
            print(f"Error in Stage 3: {e}")
            stats['errors_stage3'] += 1
            import traceback
            traceback.print_exc()
            return False

    def read_lccn_file(self, file_path):
        """Read the file containing titles and LCCNs for 505 retrieval."""
        entries = []
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                reader = csv.DictReader(file)
                
                # Make sure we have title and LCCNs
                if 'Title' not in reader.fieldnames or 'LCCN' not in reader.fieldnames:
                    print("Error: Input file must have 'Title' and 'LCCN' columns.")
                    return []
                
                for row in reader:
                    title = row.get('Title', '').strip()
                    lccn = row.get('LCCN', '').strip()
                    bibid = row.get('BibID', '').strip()
                    isbn = row.get('ISBN', '').strip()
                    
                    if title and lccn:
                        entries.append({
                            'Title': title, 
                            'LCCN': lccn,
                            'BibID': bibid,
                            'ISBN': isbn
                        })
        except Exception as e:
            print(f"Error reading LCCN file: {e}")
            return []
        
        return entries

    def fetch_marcxml(self, lccn):
        url = f"https://lccn.loc.gov/{lccn}/marcxml"
        try:
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                return response.text
            else:
                if self.args.verbose:
                    print(f"  - HTTP Status: {response.status_code}")
                return None
        except Exception as e:
            print(f"Error fetching MARCXML for LCCN {lccn}: {e}")
            return None

    def save_xml_for_debugging(self, xml_content, lccn):
        debug_dir = os.path.join(self.temp_dir, "debug_xml")
        os.makedirs(debug_dir, exist_ok=True)
        file_path = os.path.join(debug_dir, f"{lccn}.xml")
        
        with open(file_path, 'w', encoding='utf-8') as file:
            file.write(xml_content)
        
        print(f"  - Saved raw XML to {file_path}")

    def extract_505_field(self, xml_content, lccn):
        if not xml_content:
            return None
        
        if self.args.debug:
            self.save_xml_for_debugging(xml_content, lccn)
        
        try:
            namespaces = {
                'marc': 'http://www.loc.gov/MARC21/slim',
                '': ''  # Default namespace
            }
            
            root = ET.fromstring(xml_content)
            
            if self.args.verbose:
                print(f"  - XML Root tag: {root.tag}")
                print(f"  - XML Root attributes: {root.attrib}")
            
            if '}' in root.tag:
                ns = root.tag.split('}')[0] + '}'
                if self.args.verbose:
                    print(f"  - Detected namespace: {ns}")
                namespaces['marc'] = ns[1:-1]  
            
            fields_505 = []
            
            # direct search
            fields_505.extend(root.findall(".//datafield[@tag='505']"))
            
            # with namespace
            if not fields_505:
                fields_505.extend(root.findall(".//marc:datafield[@tag='505']", namespaces))
            
            # direct XPath
            if not fields_505:
                for elem in root.iter():
                    if elem.tag.endswith('datafield') and elem.get('tag') == '505':
                        fields_505.append(elem)
            
            if self.args.verbose:
                print(f"  - Found {len(fields_505)} fields with tag 505")
            
            if not fields_505:
                return None
            
            all_contents = []
            
            for field_idx, field in enumerate(fields_505):
                if self.args.verbose:
                    print(f"  - Processing 505 field #{field_idx+1}")
                    print(f"    - Field attributes: {field.attrib}")
                
                field_contents = []
                
                
                subfields = field.findall("./subfield")
                
                if not subfields:
                    subfields = field.findall("./marc:subfield", namespaces)
                
                if not subfields:
                    for elem in field.iter():
                        if elem.tag.endswith('subfield'):
                            subfields.append(elem)
                
                if self.args.verbose:
                    print(f"    - Found {len(subfields)} subfields")
                
                for subfield in subfields:
                    code = subfield.get('code', '')
                    
                    if code in ['a', 'g', 't', 'r']:
                        content = subfield.text or ""
                        if content.strip():
                            field_contents.append(content.strip())
                
                if field_contents:
                    all_contents.append(" ".join(field_contents))
            
            if all_contents:
                return "\n".join(all_contents)
            else:
                return ""
                
        except Exception as e:
            if self.args.verbose:
                print(f"  - Error extracting 505 field: {e}")
                import traceback
                traceback.print_exc()
            stats['errors_stage3'] += 1
            return None
        
    def process_505_entries(self, entries):
        with open(self.args.output, 'w', newline='', encoding='utf-8') as csv_file:
            fieldnames = ['BibID', 'Title', 'ISBN', 'LCCN', 'Status', 'Content_505']
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            
            pbar = tqdm(total=len(entries), desc="Retrieving 505 fields", unit="record")
            
            for entry in entries:
                title = entry['Title']
                lccn = entry['LCCN']
                bibid = entry['BibID']
                isbn = entry['ISBN']
                
                if not lccn:
                    writer.writerow({
                        'BibID': bibid,
                        'Title': title,
                        'ISBN': isbn,
                        'LCCN': lccn,
                        'Status': "No LCCN available",
                        'Content_505': ""
                    })
                    pbar.update(1)
                    continue
                    
                if self.args.verbose:
                    print(f"Searching for: {title} (LCCN: {lccn})")
                
                time.sleep(self.args.delay)
                
                xml_content = self.fetch_marcxml(lccn)
                
                if xml_content is None:
                    status = "Page not found or error"
                    content_505 = ""
                    stats['missing_505'] += 1
                    if self.args.verbose:
                        print(f"  - No MARCXML found")
                else:
                    if self.args.verbose:
                        print(f"  - Retrieved MARCXML content length: {len(xml_content)} characters")
                    
                    content_505 = self.extract_505_field(xml_content, lccn)
                    
                    if content_505 is None:
                        status = "No 505 tag found"
                        content_505 = ""
                        stats['missing_505'] += 1
                        if self.args.verbose:
                            print(f"  - MARCXML found but no 505 tag")
                    elif content_505 == "":
                        status = "Empty 505 tag"
                        stats['empty_505'] += 1
                        if self.args.verbose:
                            print(f"  - MARCXML found but 505 tag is empty")
                    else:
                        status = "Found"
                        stats['found_505'] += 1
                        if self.args.verbose:
                            print(f"  - MARCXML and 505 tag data found")
                            preview = content_505[:100] + "..." if len(content_505) > 100 else content_505
                            print(f"  - Preview: {preview}")
                
                # CSV out
                writer.writerow({
                    'BibID': bibid,
                    'Title': title,
                    'ISBN': isbn,
                    'LCCN': lccn,
                    'Status': status,
                    'Content_505': content_505
                })
                
                pbar.update(1)
            
            pbar.close()
            
        print(f"\nDone! Results saved to {self.args.output}")

    def print_summary(self):
        print("\n===== SUMMARY =====")
        
        if not self.args.skip_stage1:
            print("\nStage 1: Local Catalog Processing")
            print(f"Total records processed: {stats['total_records']}")
            print(f"Records with ISBN: {stats['records_with_isbn']} ({stats['records_with_isbn']/stats['total_records']*100:.1f}%)")
            print(f"Records with LCCN: {stats['records_with_lccn']} ({stats['records_with_lccn']/stats['total_records']*100:.1f}%)")
            print(f"Records with both ISBN and LCCN: {stats['records_with_both']} ({stats['records_with_both']/stats['total_records']*100:.1f}%)")
            print(f"Records with neither ISBN nor LCCN: {stats['records_with_none']} ({stats['records_with_none']/stats['total_records']*100:.1f}%)")
            print(f"Errors during processing: {stats['errors_stage1']}")
        
        if not self.args.skip_stage2:
            print("\nStage 2: LCCN Lookup")
            print(f"Items requiring LCCN lookup: {stats['items_requiring_lookup']}")
            if stats['items_requiring_lookup'] > 0:
                print(f"Successful lookups using ISBN: {stats['successful_isbn_lookups']} ({stats['successful_isbn_lookups']/stats['items_requiring_lookup']*100:.1f}%)")
                print(f"Successful lookups using title: {stats['successful_title_lookups']} ({stats['successful_title_lookups']/stats['items_requiring_lookup']*100:.1f}%)")
                print(f"Failed lookups: {stats['failed_lookups']} ({stats['failed_lookups']/stats['items_requiring_lookup']*100:.1f}%)")
        
        if not self.args.skip_stage3:
            print("\nStage 3: 505 Field Retrieval")
            print(f"Total records processed: {stats['total_505_searches']}")
            if stats['total_505_searches'] > 0:
                print(f"Records with 505 content: {stats['found_505']} ({stats['found_505']/stats['total_505_searches']*100:.1f}%)")
                print(f"Records with empty 505 tags: {stats['empty_505']} ({stats['empty_505']/stats['total_505_searches']*100:.1f}%)")
                print(f"Records with no 505 tags or errors: {stats['missing_505']} ({stats['missing_505']/stats['total_505_searches']*100:.1f}%)")
            print(f"Errors during processing: {stats['errors_stage3']}")

def main():
    parser = argparse.ArgumentParser(description='Integrated Library Catalog Tool')
    
    parser.add_argument('-i', '--input', required=True, help='Path to input CSV file')
    parser.add_argument('-o', '--output', required=True, help='Path to output CSV file')
    
    parser.add_argument('--skip-stage1', action='store_true', help='Skip Stage 1: Local catalog processing')
    parser.add_argument('--skip-stage2', action='store_true', help='Skip Stage 2: LCCN lookup')
    parser.add_argument('--skip-stage3', action='store_true', help='Skip Stage 3: 505 field retrieval')
    
    parser.add_argument('-d', '--delay', type=float, default=1.0, help='Delay between requests in seconds (default: 1.0)')
    parser.add_argument('-r', '--max-retries', type=int, default=3, help='Maximum number of retries for failed requests (default: 3)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output')
    parser.add_argument('--debug', action='store_true', help='Save raw XML for debugging')
    parser.add_argument('--clean-temp', action='store_true', help='Clean up temporary files after processing')
    
    args = parser.parse_args()
    
    processor = CatalogProcessor(args)
    return processor.run()

if __name__ == "__main__":
    sys.exit(main())
