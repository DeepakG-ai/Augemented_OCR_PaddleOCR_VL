#!/bin/bash
set -e  # exit immediately on any error

echo "========================================="
echo " Setting up vLLM locally on Ubuntu WSL2  "
echo "========================================="

# 1. Install system build tools
sudo apt update
sudo apt install -y build-essential python3-dev curl

# 2. Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh   # ✅ raw URL
source $HOME/.local/bin/env

# 3. Create Python 3.12 venv
uv venv ~/vllm_env --python 3.12 --seed

# 4. Activate the virtual environment
source ~/vllm_env/bin/activate

# 5. Export CUDA paths for WSL2
export PATH="/usr/local/cuda/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:$LD_LIBRARY_PATH"

# 6. Upgrade pip/wheel first
uv pip install --upgrade pip setuptools wheel

# 7. Install vLLM with CUDA auto-detection + timeout protection
UV_HTTP_TIMEOUT=300 uv pip install vllm --torch-backend=auto  # ✅ timeout added

echo "========================================================"
echo " Installation Complete! To start the vLLM server, run:  "
echo "========================================================"
echo ""
echo "export HUGGING_FACE_HUB_TOKEN=hf_your_token_here"
echo "source ~/vllm_env/bin/activate"
echo "python3 -m vllm.entrypoints.openai.api_server \\"
echo "  --model PaddlePaddle/PaddleOCR-VL-1.5 \\"   
echo "  --port 8001 \\"
echo "  --dtype float16 \\"
echo "  --max-model-len 2048 \\"
echo "  --gpu-memory-utilization 0.80 \\"
echo "  --max-num-seqs 4 \\"
echo "  --trust-remote-code"