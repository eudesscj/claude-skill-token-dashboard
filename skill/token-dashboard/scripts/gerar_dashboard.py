#!/usr/bin/env python3
"""
Gera um dashboard HTML de consumo de tokens a partir dos transcripts
locais do Claude Code (~/.claude/projects/**/*.jsonl).

Uso:
    python gerar_dashboard.py
    python gerar_dashboard.py --out tokens.html --dias 7

Como funciona:
    Cada linha do .jsonl é um evento (user, assistant, tool_use, etc).
    Mensagens do assistente trazem um bloco "usage" com input_tokens,
    output_tokens, cache_read_input_tokens, cache_creation_input_tokens.
    O script agrupa por sessão (arquivo) e, dentro de cada sessão,
    por "solicitação" (cada prompt real do usuário até o próximo).
"""

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


NOISE_SEGMENTS = {"c", "users", "downloads", "projetos", "documents", "desktop"}


def decode_project_name(folder_name: str) -> str:
    """O nome da pasta é o caminho do projeto com caracteres não
    alfanuméricos trocados por '-' (ex: C:\\Users\\usuario\\Downloads\\Report
    vira c--Users-usuario-Downloads-Report). Não dá pra reverter 100%,
    então extraímos os últimos segmentos com nome de conteúdo real
    (ignorando C, Users, nome de usuário, Downloads etc)."""
    parts = [p for p in folder_name.split("-") if p]
    meaningful = [p for p in parts if p.lower() not in NOISE_SEGMENTS and len(p) > 1]
    if not meaningful:
        meaningful = parts
    short = " / ".join(meaningful[-2:]) if len(meaningful) > 1 else meaningful[-1]
    return short


SYSTEM_PROMPT_PATTERNS = [
    ("<ide_opened_file>", "[IDE: arquivo aberto]"),
    ("<ide_", "[evento do IDE]"),
    ("Base directory for this skill:", "[carregamento de skill]"),
    ("Caveat:", "[aviso automático do sistema]"),
]


def clean_prompt_label(text: str) -> str:
    """Detecta prompts sintéticos (injetados pelo Claude Code, não
    digitados pelo usuário) e devolve um rótulo curto e legível.
    Prompts reais só têm quebras de linha removidas e são truncados."""
    if not text:
        return "(sem texto)"
    stripped = text.strip()
    for prefix, label in SYSTEM_PROMPT_PATTERNS:
        if stripped.startswith(prefix):
            first_line = stripped.split("\n", 1)[0]
            if prefix == "Base directory for this skill:":
                skill_path = first_line.replace(prefix, "").strip()
                skill_name = skill_path.replace("\\", "/").rstrip("/").split("/")[-1]
                return f"[carregamento de skill: {skill_name}]"
            return label
    one_line = " ".join(stripped.split())
    return one_line[:140]


def parse_timestamp(entry):
    ts = entry.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def is_real_user_prompt(entry):
    """Diferencia um prompt real do usuário de um tool_result
    (que também chega com role/type 'user' no formato da API)."""
    if entry.get("type") != "user":
        return False
    message = entry.get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        # se só tem blocos tool_result, não é prompt real
        return not all(block.get("type") == "tool_result" for block in content)
    return False


def extract_prompt_text(entry):
    message = entry.get("message", {})
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if block.get("type") == "text":
                return block.get("text", "")
    return ""


def extract_tool_calls(entry):
    tools = []
    message = entry.get("message", {})
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if block.get("type") == "tool_use":
                tools.append(block.get("name", "?"))
    return tools


def process_session_file(path: Path, cutoff: datetime):
    """Retorna dict com dados da sessão, ou None se sessão inteira
    estiver fora do período (cutoff)."""
    requests = []
    current = None
    session_last_ts = None

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = parse_timestamp(entry)
                if ts:
                    session_last_ts = ts

                if is_real_user_prompt(entry):
                    if current:
                        requests.append(current)
                    current = {
                        "prompt": clean_prompt_label(extract_prompt_text(entry)),
                        "timestamp": ts,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read": 0,
                        "cache_creation": 0,
                        "tools": defaultdict(int),
                    }
                    continue

                if entry.get("type") == "assistant" and current is not None:
                    usage = entry.get("message", {}).get("usage", {})
                    current["input_tokens"] += usage.get("input_tokens", 0) or 0
                    current["output_tokens"] += usage.get("output_tokens", 0) or 0
                    current["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
                    current["cache_creation"] += usage.get("cache_creation_input_tokens", 0) or 0
                    for tool_name in extract_tool_calls(entry):
                        current["tools"][tool_name] += 1

        if current:
            requests.append(current)

    except (IOError, OSError):
        return None

    if not requests:
        return None
    if session_last_ts and session_last_ts < cutoff:
        return None

    return {
        "session_id": path.stem,
        "requests": requests,
        "last_ts": session_last_ts,
    }


def collect_sessions(dias: int, projects_dir: Path = None):
    projects_dir = projects_dir or CLAUDE_PROJECTS_DIR
    cutoff = datetime.now(timezone.utc) - timedelta(days=dias)
    sessions = []

    if not projects_dir.exists():
        return sessions, cutoff, 0

    # Busca recursiva: funciona tanto com a estrutura padrão do Claude Code
    # (~/.claude/projects/<projeto>/<sessao>.jsonl) quanto com uma pasta
    # solta de .jsonl vindos de upload/zip extraído.
    for jsonl_file in projects_dir.rglob("*.jsonl"):
        project_folder = jsonl_file.parent.name
        project_name = decode_project_name(project_folder)
        data = process_session_file(jsonl_file, cutoff)
        if data:
            data["project"] = project_name
            sessions.append(data)

    total_before = len(sessions)
    sessions = [
        s for s in sessions
        if sum(r["input_tokens"] + r["output_tokens"] + r["cache_creation"] + r["cache_read"] for r in s["requests"]) > 0
    ]
    skipped = total_before - len(sessions)

    sessions.sort(key=lambda s: s["last_ts"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return sessions, cutoff, skipped


def fmt(n):
    return f"{n:,}".replace(",", ".")


def build_html(sessions, dias, out_path, skipped=0):
    now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    max_new_tokens = 1
    for s in sessions:
        for r in s["requests"]:
            new_tokens = r["input_tokens"] + r["output_tokens"] + r["cache_creation"]
            max_new_tokens = max(max_new_tokens, new_tokens)

    # agrupa por projeto, e dentro do projeto ordena sessões da mais recente pra mais antiga
    by_project = defaultdict(list)
    for s in sessions:
        by_project[s["project"]].append(s)

    def project_last_activity(project_sessions):
        tss = [s["last_ts"] for s in project_sessions if s["last_ts"]]
        return max(tss) if tss else datetime.min.replace(tzinfo=timezone.utc)

    project_blocks = []
    for project, project_sessions in sorted(by_project.items(), key=lambda kv: project_last_activity(kv[1]), reverse=True):
        project_sessions.sort(key=lambda s: s["last_ts"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        project_total = sum(
            r["input_tokens"] + r["output_tokens"] + r["cache_creation"]
            for s in project_sessions for r in s["requests"]
        )

        session_blocks = []
        for s in project_sessions:
            total_new = sum(r["input_tokens"] + r["output_tokens"] + r["cache_creation"] for r in s["requests"])
            total_output = sum(r["output_tokens"] for r in s["requests"])
            total_cache_read = sum(r["cache_read"] for r in s["requests"])
            last_ts_str = s["last_ts"].astimezone().strftime("%d/%m/%Y %H:%M") if s["last_ts"] else "-"

            request_rows = []
            for r in s["requests"]:
                new_tokens = r["input_tokens"] + r["output_tokens"] + r["cache_creation"]
                pct = int(100 * new_tokens / max_new_tokens) if max_new_tokens else 0
                pct = max(pct, 2)

                total_bar = r["input_tokens"] + r["output_tokens"] + r["cache_creation"]
                in_pct = (r["input_tokens"] / total_bar * 100) if total_bar else 0
                out_pct = (r["output_tokens"] / total_bar * 100) if total_bar else 0
                cache_pct = (r["cache_creation"] / total_bar * 100) if total_bar else 0

                tools_str = ", ".join(f"{name}×{count}" for name, count in sorted(r["tools"].items()))
                prompt_escaped = (r["prompt"] or "(sem texto)").replace("<", "&lt;").replace(">", "&gt;")
                cache_read_str = f' · cache lido {fmt(r["cache_read"])}' if r["cache_read"] else ""

                request_rows.append(f"""
                <div class="request-row">
                    <div class="request-top">
                        <div class="request-label" title="{prompt_escaped}">{prompt_escaped}</div>
                        <div class="request-numbers">
                            <span class="num-total">{fmt(new_tokens)}</span>
                            <span class="num-output">saída {fmt(r['output_tokens'])}{cache_read_str}</span>
                        </div>
                    </div>
                    <div class="bar-track" style="width:{pct}%" title="entrada {fmt(r['input_tokens'])} · saída {fmt(r['output_tokens'])} · cache criado {fmt(r['cache_creation'])}">
                        <div class="bar-seg in" style="width:{in_pct:.1f}%"></div>
                        <div class="bar-seg out" style="width:{out_pct:.1f}%"></div>
                        <div class="bar-seg cache" style="width:{cache_pct:.1f}%"></div>
                    </div>
                    {f'<div class="tools">{tools_str}</div>' if tools_str else ''}
                </div>
                """)

            session_blocks.append(f"""
            <div class="session-card">
                <div class="session-header">
                    <div class="session-id">Sessão {s['session_id'][:8]}
                        <span class="session-meta">{len(s['requests'])} solicitação(ões) · <strong>{fmt(total_new)}</strong> tokens novos · saída {fmt(total_output)}{f' · cache lido {fmt(total_cache_read)}' if total_cache_read else ''}</span>
                    </div>
                    <div class="session-date">🕐 {last_ts_str}</div>
                </div>
                {''.join(request_rows)}
            </div>
            """)

        project_blocks.append(f"""
        <div class="project-group">
            <div class="project-header">
                <span class="project-name">{project}</span>
                <span class="project-total">{fmt(project_total)} tokens novos no total</span>
            </div>
            {''.join(session_blocks)}
        </div>
        """)

    session_blocks = project_blocks

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Consumo de tokens — Claude Code</title>
<style>
  :root {{
    --bg: #0d1117; --card: #131a21; --border: #232c36;
    --text: #d6dee6; --muted: #7d8a96;
    --blue: #4c8fe0; --orange: #d98c3f; --green: #3fb68f;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    margin: 0; padding: 24px;
  }}
  .wrap {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-bottom: 18px; }}
  .legend {{ display: flex; gap: 18px; align-items: center; font-size: 12px; color: var(--muted); margin-bottom: 20px; }}
  .legend .dot {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; margin-right: 5px; vertical-align: middle; }}
  .dot.in {{ background: var(--blue); }}
  .dot.out {{ background: var(--orange); }}
  .dot.cache {{ background: var(--green); }}
  .project-group {{ margin-bottom: 28px; }}
  .project-header {{
    display: flex; justify-content: space-between; align-items: baseline;
    padding: 4px 2px 10px; border-bottom: 2px solid var(--blue);
    margin-bottom: 12px;
  }}
  .project-name {{ font-size: 17px; font-weight: 700; color: var(--text); }}
  .project-total {{ font-size: 13px; color: var(--muted); }}
  .session-card {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px 18px; margin-bottom: 12px;
  }}
  .session-header {{
    display: flex; justify-content: space-between; align-items: baseline;
    border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-bottom: 10px;
  }}
  .session-id {{ font-size: 14px; font-weight: 600; }}
  .session-meta {{ font-weight: normal; color: var(--muted); font-size: 13px; margin-left: 10px; }}
  .session-date {{ color: var(--muted); font-size: 13px; }}
  .request-row {{ padding: 10px 0; border-bottom: 1px solid #1a212a; }}
  .request-row:last-child {{ border-bottom: none; }}
  .request-top {{ display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 6px; }}
  .request-label {{ font-size: 15px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .request-numbers {{ white-space: nowrap; font-size: 14px; }}
  .num-total {{ font-weight: 700; color: var(--text); margin-right: 8px; }}
  .num-output {{ color: var(--orange); font-size: 13px; }}
  .bar-track {{ height: 9px; border-radius: 4px; overflow: hidden; display: flex; background: #1a212a; min-width: 20px; }}
  .bar-seg {{ height: 100%; }}
  .bar-seg.in {{ background: var(--blue); }}
  .bar-seg.out {{ background: var(--orange); }}
  .bar-seg.cache {{ background: var(--green); }}
  .tools {{ font-size: 12.5px; color: var(--muted); margin-top: 6px; }}
  .empty {{ text-align: center; color: var(--muted); padding: 60px 0; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Consumo de tokens por solicitação</h1>
  <div class="subtitle">Gerado em <strong>{now_str}</strong> · {len(sessions)} sessão(ões) com uso real nos últimos {dias} dia(s){f' · {skipped} sessão(ões) sem consumo de tokens ocultadas' if skipped else ''} · agrupado por projeto.</div>
  <div class="legend">
    <span><span class="dot in"></span>entrada</span>
    <span><span class="dot out"></span>saída (mais cara)</span>
    <span><span class="dot cache"></span>cache criado</span>
    <span style="margin-left:auto">cache lido não entra na barra (barato)</span>
  </div>
  {''.join(session_blocks) if session_blocks else '<div class="empty">Nenhuma sessão do Claude Code encontrada no período.</div>'}
</div>
</body>
</html>"""

    Path(out_path).write_text(html, encoding="utf-8")


def resolve_input_dir(input_arg: str) -> Path:
    """Aceita: pasta com .jsonl (recursivo), um único arquivo .jsonl,
    ou um .zip (extrai pra pasta temporária e devolve o caminho)."""
    import tempfile
    import zipfile

    path = Path(input_arg)

    if path.suffix.lower() == ".zip":
        tmp_dir = Path(tempfile.mkdtemp(prefix="token_dashboard_"))
        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(tmp_dir)
        return tmp_dir

    if path.is_file() and path.suffix.lower() == ".jsonl":
        # arquivo único: cria uma pasta temp com ele dentro, mantendo
        # o nome do arquivo original como "sessão" e a pasta pai como "projeto"
        tmp_dir = Path(tempfile.mkdtemp(prefix="token_dashboard_"))
        project_dir = tmp_dir / (path.parent.name or "upload")
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / path.name).write_bytes(path.read_bytes())
        return tmp_dir

    return path


def main():
    parser = argparse.ArgumentParser(description="Dashboard de tokens do Claude Code")
    parser.add_argument("--out", default="tokens.html", help="Arquivo HTML de saída")
    parser.add_argument("--dias", type=int, default=7, help="Janela de dias pra considerar sessões")
    parser.add_argument(
        "--input", default=None,
        help="Pasta, .jsonl único ou .zip com os transcripts. Padrão: ~/.claude/projects"
    )
    args = parser.parse_args()

    projects_dir = resolve_input_dir(args.input) if args.input else None

    sessions, _, skipped = collect_sessions(args.dias, projects_dir)
    build_html(sessions, args.dias, args.out, skipped)
    print(f"Gerado: {args.out} ({len(sessions)} sessões, {skipped} ignoradas por não ter uso)")


if __name__ == "__main__":
    main()
