import asyncio
import base64
import os
import sys

# Ensure we can import from qwen_backend
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from qwen_backend.processor import pdf_to_images

async def main():
    input_dir = r"C:\Users\aigroup5\Downloads\PDF Samples\input"
    
    # Create the output folder in the current directory
    output_dir = os.path.join(os.getcwd(), "output")
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"Reading PDFs from: {input_dir}")
    print(f"Saving images to : {output_dir}")
    print("-" * 40)
    
    if not os.path.exists(input_dir):
        print(f"Error: Input directory does not exist: {input_dir}")
        return

    # Process all PDF files
    files_processed = 0
    for filename in os.listdir(input_dir):
        if not filename.lower().endswith(".pdf"):
            continue
            
        pdf_path = os.path.join(input_dir, filename)
        print(f"Processing: {filename}...")
        
        # Read the file
        with open(pdf_path, "rb") as f:
            file_bytes = f.read()
            
        # Call our async processor
        try:
            pages = await pdf_to_images(file_bytes)
            
            # Save each page as an image
            for page in pages:
                # The page dict schema: {page_number, image_b64, mime_type, width, height}
                page_num = page["page_number"]
                b64_str = page["image_b64"]
                
                # Decode base64 to bytes
                image_bytes = base64.b64decode(b64_str)
                
                # Write to output folder
                out_filename = f"{os.path.splitext(filename)[0]}_page_{page_num}.jpg"
                out_path = os.path.join(output_dir, out_filename)
                
                with open(out_path, "wb") as f_out:
                    f_out.write(image_bytes)
                    
                print(f"  -> Saved page {page_num}: {out_filename} ({page['width']}x{page['height']} px)")
            
            files_processed += 1
        except Exception as e:
            print(f"  -> Error processing {filename}: {e}")
            
    print("-" * 40)
    print(f"Done! Processed {files_processed} PDFs.")

if __name__ == "__main__":
    asyncio.run(main())