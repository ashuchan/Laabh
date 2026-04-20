#!/bin/bash
set -e

# Check GPU availability, fall back to CPU gracefully
if ! nvidia-smi &>/dev/null; then
    echo "WARNING: GPU not available, using CPU for Whisper (slower)"
    export WHISPER_DEVICE=cpu
fi

echo "Whisper device: ${WHISPER_DEVICE:-cuda}"
exec "$@"
