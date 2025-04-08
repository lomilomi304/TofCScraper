# TofCScraper

A utility for fetching LCCNs & ISBNs from BibIDs, and extracting MARC 505 content based on LCCNs and ISBNs

## Installation

### Prerequisites

- Python

packages: requests beautifulsoup4 tqdm

## Usage

### Basic Usage

```
python TofCScraper.py -i input.csv -o output.csv
```

### Command-line Arguments

| Argument | Description |
|----------|-------------|
| `-i, --input` | Path to input CSV file |
| `-o, --output` | Path to output CSV file |
| `--skip-stage1` | Skip Stage 1: Local catalog processing |
| `--skip-stage2` | Skip Stage 2: LCCN lookup |
| `--skip-stage3` | Skip Stage 3: 505 field retrieval |
| `-d, --delay` | Delay between requests in seconds (default: 1.0) |
| `-r, --max-retries` | Maximum retries for failed requests (default: 3) |
| `-v, --verbose` | Enable verbose output |
| `--debug` | Save raw XML for debugging |
| `--clean-temp` | Clean up temporary files after processing |

### Example args

**Only retrieve 505 fields (using existing LCCNs):**
```
python TofCScraper.py -i records_with_lccns.csv -o toc_data.csv --skip-stage1 --skip-stage2
```

**Process with longer delays between requests:**
```
python TofCScraper.py -i my_catalog.csv -o final_results.csv -d 5.0
```

## Input/Output Formats

### Input CSV Format (for full process)

The input CSV should have at minimum:
- A column containing BibIDs
- A column containing titles

Example:
```csv
BibID,Title
12345,The Great Gatsby
67890,To Kill a Mockingbird
```

### Output CSV Format

The final output CSV contains:
- BibID: The library's unique identifier
- Title: The book title
- ISBN: International Standard Book Number(s)
- LCCN: Library of Congress Control Number
- Status: Result of 505 field retrieval
- Content_505: The contents of the 505 field (table of contents)

## Process

### Stage 1: Local Catalog Processing
1. Reads the input CSV file with BibIDs and titles
2. For each record, scrapes the OPAC page
3. Extracts ISBNs and LCCNs from MARC data
4. Saves results to a temporary CSV file

### Stage 2: LCCN Lookup
1. Reads the Stage 1 output (or a provided CSV file)
2. For records missing an LCCN, attempts to find it via:
   - First trying an ISBN-based search at the Library of Congress
   - If unsuccessful, trying a title-based search (only works if single search result is returned)
3. Saves results to a temporary CSV file

### Stage 3: 505 Field Retrieval
1. Reads the Stage 2 output or a provided CSV file
2. For each record with an LCCN, retrieves the MARCXML from LC
3. Extracts the 505 field (table of contents) content (if exists)
4. Writes all data to the final output CSV file

## Troubleshooting

### Debugging

When using the `--debug` flag, raw XML responses are saved to a `temp/debug_xml` directory
