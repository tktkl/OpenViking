"""Optional external-task watcher. No Codex or chat-vendor imports belong here."""

from vikingbot.extensions.background_tasks.service import BackgroundTasks


def create_extension(context, options):
    """Runtime-extension factory selected explicitly in deployment config."""
    return BackgroundTasks(context, options)
