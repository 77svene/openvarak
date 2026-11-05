---
title: Deployment
description: Deploy OpenVarak in production environments
---

# Deployment

OpenVarak supports multiple deployment strategies for different environments
and scales.

## Docker

The recommended way to deploy OpenVarak in production. Multi-stage builds
with CPU and GPU (NVIDIA CUDA, AMD ROCm) variants.

[:octicons-arrow-right-24: Docker deployment](docker.md)

## systemd (Linux)

Run OpenVarak as a managed system service on Linux servers.

[:octicons-arrow-right-24: systemd setup](systemd.md)

## launchd (macOS)

Register OpenVarak as a launch agent on macOS.

[:octicons-arrow-right-24: launchd setup](launchd.md)

## API Server

Run OpenVarak as an OpenAI-compatible HTTP server via `jarvis serve`.

[:octicons-arrow-right-24: API server guide](api-server.md)
