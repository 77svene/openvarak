"""Primary varak CLI. Academic/legacy commands remain on the jarvis alias."""

from __future__ import annotations

import json
import os
from importlib.resources import files
from pathlib import Path

import click

from openvarak import __version__


def desktop_config(path=None):
    from openvarak.core.config import load_config
    from openvarak.core.paths import get_config_path

    # Explicit file > env file > installed config > bundled desktop profile.
    explicit = path or os.environ.get("OPENVARAK_CONFIG")
    if explicit:
        selected = Path(explicit).expanduser()
        if not selected.is_file():
            raise click.ClickException(f"Config file not found: {selected}")
        return load_config(selected)
    user_config = get_config_path()
    if user_config.exists():
        return load_config(user_config)
    profile = files("openvarak").joinpath("data/desktop-autonomous.toml")
    return load_config(Path(str(profile)))


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="varak")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.pass_context
def main(ctx, config_path):
    """OpenVarak — single-desktop autonomous agent runtime.

    Bare varak starts the loopback webhook daemon. Local inference needs a
    running Ollama/vLLM/SGLang/llama.cpp server and an installed tool-capable model.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    if ctx.invoked_subcommand is None:
        ctx.invoke(daemon)


@main.command()
@click.pass_context
def init(ctx):
    """Install the desktop profile without consumer/cron presets."""
    from openvarak.core.paths import get_config_dir, get_config_path

    destination = ctx.obj["config_path"] or get_config_path()
    if destination.exists():
        raise click.ClickException(f"Config already exists: {destination}")
    get_config_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        files("openvarak")
        .joinpath("data/desktop-autonomous.toml")
        .read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    click.echo(f"Installed desktop profile: {destination}")


@main.command()
@click.pass_context
def check(ctx):
    """Verify config and local FTS5 storage without inference/network calls."""
    from openvarak.engine.desktop import resolve_endpoint
    from openvarak.tools.storage.persistent import HybridPersistentMemoryBackend

    config = desktop_config(ctx.obj["config_path"])
    endpoint = resolve_endpoint(
        getattr(config.engine, f"{config.engine.default}_host"), config.cluster
    )
    memory = HybridPersistentMemoryBackend(config.memory.db_path)
    try:
        click.echo(
            json.dumps(
                {
                    "package": "openvarak",
                    "endpoint": endpoint,
                    "memory": "sqlite-fts5",
                    "documents": memory.count(),
                    "telemetry": config.telemetry.enabled,
                    "cluster": config.cluster.enabled,
                }
            )
        )
    finally:
        memory.close()


@main.command()
@click.option("--port", type=click.IntRange(1, 65535), default=None)
@click.pass_context
def daemon(ctx, port):
    """Serve /webhooks/trigger on 127.0.0.1; no cron/discovery required."""
    try:
        import uvicorn

        from openvarak.runtime.daemon import create_app
    except ImportError as exc:
        raise click.ClickException(
            "Install daemon dependencies: uv sync --extra server"
        ) from exc
    config = desktop_config(ctx.obj["config_path"])
    click.echo(
        f"OpenVarak listening on http://127.0.0.1:{port or config.desktop.webhook_port}"
    )
    uvicorn.run(
        create_app(config), host="127.0.0.1", port=port or config.desktop.webhook_port
    )


@main.command()
@click.argument("prompt")
@click.pass_context
def ask(ctx, prompt):
    """Run one task with the same engine, memory, and tools as the daemon."""
    import asyncio

    from openvarak.runtime.daemon import DesktopRuntime

    async def run():
        runtime = DesktopRuntime(desktop_config(ctx.obj["config_path"]))
        try:
            await runtime.start()
            job_id = runtime.submit({"prompt": prompt})
            await runtime._queue.join()
            job = runtime.job(job_id)
            if job["status"] != "completed":
                raise click.ClickException(job["result"]["error"])
            click.echo(job["result"]["content"])
        finally:
            await runtime.close()

    asyncio.run(run())
