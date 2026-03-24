# Proto Augment PaddleOCR — Standalone VLM Tester

Standalone folder for testing PaddleOCR-VL model responses directly. 
No connection to the main Augmented OCR app.

## Setup

```bash
pip install requests Pillow PyMuPDF
```

## Usage

```bash
# Test with a PDF — see what the model returns for each field on each page
python run_extraction.py path/to/invoice.pdf

# Test specific fields only
python run_extraction.py path/to/invoice.pdf --fields vendor_name,invoice_number

# Test with a single image
python run_extraction.py path/to/invoice.png

# Change vLLM endpoint (default: http://localhost:8001)
python run_extraction.py invoice.pdf --url http://localhost:8001
```

## What it does

1. Opens PDF → converts each page to an image
2. For each page × each field: sends to PaddleOCR-VL via vLLM
3. Prints the **raw model response** so you can see exactly what it returns
4. No parsing, no filtering — raw output only
