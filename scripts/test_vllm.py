from __future__ import annotations

from paddleocr import PaddleOCRVL


def run_demo() -> None:
    pipeline = PaddleOCRVL(
        vl_rec_backend="vllm-server",
        vl_rec_server_url="http://localhost:8008/v1/chat/completions",
    )

    output = pipeline.predict(
        r"C:\Users\aigroup5\Pictures\Screenshots\Screenshot 2026-03-24 172803.png"
    )
    for res in output:
        res.print()
        res.save_to_json(save_path="output")
        res.save_to_markdown(save_path="output")


if __name__ == "__main__":
    run_demo()
