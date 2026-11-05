# Pearl Tooling

This directory holds standalone Pearl ecosystem utilities that are useful during
model enablement or validation but are not part of the OpenVarak runtime.

- `model_converter.py` creates experimental Pearl-compatible staging
  checkpoints from raw Hugging Face safetensors models.

Keep user-facing mining commands in `src/openvarak/cli/` and runtime provider
code in `src/openvarak/mining/`. Scripts here should be explicit operational
tools that developers run manually.
