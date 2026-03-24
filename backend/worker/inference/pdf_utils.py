import fitz  # PyMuPDF
import numpy as np

# PDF processing limits
MAX_FILE_SIZE_MB = 50
MAX_PAGES = 100


def pdf_to_numpy_pages(file_bytes: bytes, dpi: int = 300) -> list[np.ndarray]:
    """Burst a PDF into a list of numpy arrays, one per page at 300 DPI."""
    # Validate file size
    if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
        raise ValueError(
            f"File too large: {len(file_bytes) / (1024 * 1024):.1f}MB. "
            f"Max allowed: {MAX_FILE_SIZE_MB}MB"
        )

    doc = fitz.open(stream=file_bytes, filetype="pdf")

    # Validate page count
    if len(doc) > MAX_PAGES:
        doc.close()
        raise ValueError(
            f"Too many pages: {len(doc)}. Max allowed: {MAX_PAGES}"
        )

    pages = []
    for page in doc:
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        if pix.n == 4:  # RGBA → RGB
            img = img[:, :, :3]
        pages.append(img)
    doc.close()
    return pages


def image_bytes_to_numpy(file_bytes: bytes) -> list[np.ndarray]:
    """Wrap a single image into a list for uniform processing."""
    import cv2

    arr = np.frombuffer(file_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return [img]
