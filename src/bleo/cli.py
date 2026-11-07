from __future__ import annotations

import argparse
from http.client import HTTPException
import sys

from . import Bleo, BleoError, __version__
from .models import DefaultModel, MODEL_ALIASES, list_models, model_info, refresh_models, resolve_model
from .ui import PickerItem, Theme, UI, image_data_url, read_text_file, truncated

THEME = Theme("Bleo", "🦁", 196, "Brave Leo")
ALIASES: dict[str, str] = {}
for _alias, _model in MODEL_ALIASES.items():
    ALIASES.setdefault(_model, _alias)


def main() -> None:
    parser = argparse.ArgumentParser(prog="bleo", description="Brave Leo AI Chat")
    parser.add_argument("prompt_text", nargs="?", help="Prompt; omit for interactive chat")
    parser.add_argument("-p", "--prompt", help="Prompt; same as the positional form")
    parser.add_argument("-m", "--model", default=DefaultModel,
                        help=f"Model name or alias (default: {DefaultModel})")
    parser.add_argument("--models", "--list-models", dest="models", action="store_true",
                        help="List models (free by default) and exit")
    parser.add_argument("--models-all", action="store_true",
                        help="List every model, including premium")
    parser.add_argument("--refresh-models", action="store_true",
                        help="Fetch and print Brave's current model catalog")
    parser.add_argument("--access", choices=("free", "premium"), help="Filter model list")
    parser.add_argument("-k", "--x-brave-key", help="Also BRAVE_X_BRAVE_KEY")
    parser.add_argument("--pre-shared-key", help="Also BRAVE_PRE_SHARED_KEY")
    parser.add_argument("--signing-key-id", help="Also BRAVE_SIGNING_KEY_ID")
    parser.add_argument("--proxy", help="HTTP or HTTPS proxy URL")
    parser.add_argument("--history", action="store_true", help="Remember conversation turns")
    parser.add_argument("--no-stream", action="store_true", help="Print the complete reply")
    parser.add_argument("--image", help="Attach a local image to a one-shot prompt")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colour")
    parser.add_argument("--version", action="version", version=f"bleo {__version__}")
    args = parser.parse_args()

    ui = UI(THEME, enabled=False if args.no_color else None)
    prompt = args.prompt if args.prompt is not None else args.prompt_text
    try:
        if args.refresh_models:
            refresh_models()
        if args.models or args.models_all or args.refresh_models:
            access = args.access
            if access is None and args.models and not args.models_all:
                access = "free"
            _print_models(access, ui, prompt=args.model)
            return
        with Bleo(model=args.model, x_brave_key=args.x_brave_key,
                  pre_shared_key=args.pre_shared_key, signing_key_id=args.signing_key_id,
                  proxy=args.proxy, history=args.history or prompt is None) as client:
            if prompt is not None or args.image:
                _print_reply(client, prompt or "Describe this image.", args.no_stream, ui,
                             image=args.image)
            else:
                _repl(client, args.no_stream, ui)
    except (BleoError, OSError, HTTPException, ValueError) as error:
        ui.error(str(error), prefix="bleo")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print(file=sys.stderr)
        raise SystemExit(130) from None


def _model_choices(access=None):
    choices = []
    for name in list_models(access):
        info = model_info(name) or {}
        alias = ALIASES.get(name, "")
        compatibility = not info.get("advertised", True)
        label = name
        display = info.get("name")
        if display and display != name:
            label += f" · {display}"
        if alias:
            label += f" ({alias})"
        if compatibility:
            label += " *"
        capabilities = info.get("capabilities") or []
        tags = []
        if "vision" in capabilities:
            tags.append("vision")
        if "tools" in capabilities:
            tags.append("tools")
        if "deep_research" in capabilities:
            tags.append("research")
        choices.append({
            "id": name,
            "label": label,
            "access": "premium" if info.get("access") == "premium" else "free",
            "caps": ", ".join(tags),
            "compatibility": compatibility,
        })
    return choices


def _print_models(access=None, ui=None, prompt=None):
    ui = ui or UI(THEME)
    choices = _model_choices(access)
    current = resolve_model(prompt) if prompt else DefaultModel
    rows = []
    for index, choice in enumerate(choices, 1):
        mark = ui.accent("●") if choice["id"] == current else " "
        label = choice["label"]
        rows.append([
            mark,
            ui.dim(str(index)),
            ui.bold(label) if choice["id"] == current else label,
            choice["access"],
            ui.dim(choice["caps"]),
        ])
    footer = f"Current: {current} · switch with /model <number|alias|id>"
    if any(choice["compatibility"] for choice in choices):
        footer += " · * compatibility ID"
    if not choices:
        ui.hint(f"No model matches {access!r}.")
    else:
        title = f"Models · {THEME.name}" + (f" · {access}" if access else "")
        ui.table(title, ["", "#", "MODEL", "ACCESS", "CAPABILITIES"], rows,
                 align_right=(1,), footer=footer)
    return choices


def _pick_model(ui, choices, current, title, access=None):
    if not choices:
        ui.hint("No models available.")
        return None
    if not ui.interactive:
        _print_models(access, ui, prompt=current)
        ui.hint("Interactive picker needs a terminal; use /model <number|alias|id>.")
        return None
    items = []
    for choice in choices:
        detail = choice["access"]
        if choice["caps"]:
            detail += f" · {choice['caps']}"
        items.append(PickerItem(value=choice["id"], label=choice["label"], detail=detail))
    return ui.pick(title, items, current=current)


def _parse_choice(token, choices):
    token = token.strip()
    if not token:
        raise ValueError("model name is empty")
    if token.isdigit():
        index = int(token)
        if not 1 <= index <= len(choices):
            raise ValueError(f"model number must be between 1 and {len(choices)}")
        return choices[index - 1]["id"], True
    selected = resolve_model(token)
    known = {choice["id"] for choice in choices}
    return selected, selected in known


def _switch_model(client, token, choices, ui):
    try:
        selected, known = _parse_choice(token, choices)
    except ValueError as error:
        ui.error(str(error), prefix="bleo")
        return False
    client.model = selected
    client.history.model = selected
    if known:
        ui.success(f"model → {selected}")
    else:
        ui.hint(f"model → {selected} (not in the catalog; the request may fail)")
    return True


def _print_reply(client, prompt, no_stream=False, ui=None, image=None):
    ui = ui or UI(THEME)
    if image:
        print(client.ask_with_image(prompt, image_data_url(image)))
        return
    if no_stream:
        print(client.ask(prompt))
    else:
        for chunk in client.stream(prompt):
            print(chunk, end="", flush=True)
        print()


def _print_history(client, ui):
    if not client.history.messages:
        ui.hint("(history is empty)")
        return
    ui.section("History")
    for message in client.history.messages:
        content = message.content
        if isinstance(content, list):
            content = " ".join(str(part) for part in content)
        print(f"  {ui.accent(message.role + ':')} {truncated(str(content), 200)}", file=ui.out)
    print(file=ui.out)


def _status(client, ui):
    rows = [
        ["provider", "Brave Leo"],
        ["model", client.model],
        ["served as", client.last_model or "—"],
        ["history", "on" if client.history_enabled else "off"],
    ]
    ui.table("Status", ["SETTING", "VALUE"], rows)


def _help(ui, client):
    ui.commands((
        ("/help", "Show this help."),
        ("/models", "Pick a free model interactively."),
        ("/models-all", "Pick from all models, including premium."),
        ("/model [N|name]", "Show or switch the active model."),
        ("/status", "Show model, served-as and history state."),
        ("/history [on|off]", "Show the conversation or toggle memory."),
        ("/image PATH [text]", "Send a local image."),
        ("/read FILE", "Send a text file as the next prompt."),
        ("/clear", "Clear conversation history."),
        ("/quit, /exit", "Leave the prompt."),
    ))
    ui.hint(f"Current model: {client.model} · /models lists free models")


def _repl(client, no_stream=False, ui=None):
    ui = ui or UI(THEME)
    commands = (
        "/help", "/models", "/models-all", "/model", "/status", "/history",
        "/image", "/read", "/clear", "/quit", "/exit",
    )
    ui.banner(
        __version__,
        facts=(
            f"model {client.model}",
            f"history {'on' if client.history_enabled else 'off'}",
            "provider Brave Leo",
        ),
        hint="/help · /models free · /models-all all · Ctrl+D quit",
    )
    while True:
        try:
            prompt = ui.readline(ui.prompt(), completions=commands).strip()
            if not prompt:
                continue
            if prompt.startswith("/"):
                if not _handle_repl_command(client, prompt, no_stream, ui):
                    ui.hint("Bye.")
                    return
            else:
                _print_reply(client, prompt, no_stream, ui)
        except EOFError:
            print(file=ui.out)
            return
        except KeyboardInterrupt:
            print(file=ui.out)
        except (BleoError, OSError, HTTPException, UnicodeError, ValueError) as error:
            ui.error(str(error), prefix="bleo")


def _handle_repl_command(client, raw, no_stream=False, ui=None):
    ui = ui or UI(THEME)
    parts = raw[1:].strip().split(maxsplit=1)
    if not parts:
        return True
    command = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if command in ("quit", "exit", "q"):
        return False
    if command in ("help", "h", "?"):
        _help(ui, client)
    elif command in ("models", "models-all"):
        show_all = command == "models-all"
        access = None if show_all else "free"
        choices = _model_choices(access)
        if arg:
            _switch_model(client, arg, choices, ui)
        else:
            title = f"Models · {THEME.name} · {'all' if show_all else 'free'}"
            selected = _pick_model(ui, choices, client.model, title, access)
            if selected:
                _switch_model(client, selected, choices, ui)
    elif command == "model":
        if not arg:
            choices = _model_choices("free")
            selected = _pick_model(
                ui, choices, client.model, f"Models · {THEME.name} · free", "free"
            )
            if selected:
                _switch_model(client, selected, choices, ui)
        else:
            choices = _model_choices()
            _switch_model(client, arg, choices, ui)
    elif command == "status":
        _status(client, ui)
    elif command == "clear":
        client.reset()
        ui.success("history cleared")
    elif command == "history":
        if arg.lower() in ("on", "off"):
            if arg.lower() == "on":
                client.enable_history()
            else:
                client.disable_history()
            ui.success(f"history {'on' if client.history_enabled else 'off'}")
        else:
            _print_history(client, ui)
    elif command == "read":
        if not arg:
            ui.error("usage: /read FILE", prefix="bleo")
        else:
            try:
                _print_reply(client, read_text_file(arg), no_stream, ui)
            except (OSError, ValueError) as error:
                ui.error(str(error), prefix="bleo")
    elif command == "image":
        if not arg:
            ui.error("usage: /image PATH [prompt]", prefix="bleo")
        else:
            path, _, caption = arg.partition(" ")
            try:
                _print_reply(client, caption.strip() or "Describe this image.", no_stream, ui, image=path)
            except (OSError, ValueError) as error:
                ui.error(str(error), prefix="bleo")
    else:
        ui.error(f"unknown command: /{command}", prefix="bleo")
    return True


if __name__ == "__main__":
    main()
