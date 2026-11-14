"""Command line interface.

  anon setup-models                      download NER weights to ./models (only command with network)
  anon review  IN.docx [--entities E.xlsx] [--no-llm] [--no-ner] [--config rules.yaml] -o OUTDIR
  anon finalize REVIEW.docx --entities E.xlsx -o OUT.docx [--keep-entities]
  anon evaluate --pairs testdata/ [--no-llm] [-o eval_out]
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional

import typer

from .config import load_config

# show_locals=False: a crash must never print document text from local variables
app = typer.Typer(add_completion=False, no_args_is_help=True, pretty_exceptions_show_locals=False,
                  help="On-prem anonymisation of court decisions.")
log = logging.getLogger("anon")


def _setup_logging(verbose: bool) -> None:
    # Logs carry counts and categories only, never document text.
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def _offline() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["DO_NOT_TRACK"] = "1"


def _llm_client(cfg, no_llm: bool):
    if no_llm or not cfg.rules.get("llm", {}).get("enabled", False):
        return None
    from .detect.llm import LLMClient, LLMUnavailable  # noqa: PLC0415

    try:
        client = LLMClient.from_config(cfg)
        client.ping()
        return client
    except LLMUnavailable as exc:
        typer.secho(f"WARNUNG: LLM nicht erreichbar ({exc}); weiter ohne LLM (--no-llm).", fg="yellow", err=True)
        return None


@app.command()
def review(
    input: Path = typer.Argument(..., exists=True, dir_okay=False, help="Original decision (.docx)"),
    out: Path = typer.Option(..., "-o", "--out", help="Output directory"),
    entities: Optional[Path] = typer.Option(None, "--entities", exists=True, help="Edited entities.xlsx"),
    config: Optional[Path] = typer.Option(None, "--config", exists=True, help="Rules override (YAML)"),
    no_llm: bool = typer.Option(False, "--no-llm", help="Do not use the local LLM"),
    no_ner: bool = typer.Option(False, "--no-ner", help="Do not use the NER model"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Create the review copy (tracked changes + comments) and the entities file."""
    _setup_logging(verbose)
    _offline()
    from .pipeline import review as run_review  # noqa: PLC0415

    cfg = load_config(config)
    llm = _llm_client(cfg, no_llm)
    analysis, docx, xlsx = run_review(input, cfg, out, use_ner=not no_ner, entities_path=entities, llm=llm)
    s = analysis.stats
    typer.echo(f"Prüfkopie:  {docx}")
    typer.echo(f"Entitäten:  {xlsx}  (enthält Originalwerte)")
    typer.echo(f"{s['entities']} Entitäten, {s['changes']} Änderungen, {s['comments']} Prüfhinweise; "
               f"NER: {s['ner']}; LLM: {'an' if llm else 'aus'}")
    typer.echo("Nach Kategorie: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_category"].items())))


@app.command()
def finalize(
    review_docx: Path = typer.Argument(..., exists=True, dir_okay=False, help="Reviewed copy (.docx)"),
    entities: Path = typer.Option(..., "--entities", exists=True, help="entities.xlsx of this decision"),
    out: Path = typer.Option(..., "-o", "--out", help="Clean output .docx"),
    config: Optional[Path] = typer.Option(None, "--config", exists=True),
    keep_entities: bool = typer.Option(False, "--keep-entities", help="Evaluation only: do not delete entities file"),
    show_values: bool = typer.Option(False, "--show-values", help="Print leaked values to the console (not the log)"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Accept remaining changes, strip review artefacts and metadata, run the leak check."""
    _setup_logging(verbose)
    from .pipeline import finalize as run_finalize  # noqa: PLC0415
    from .review.entities_file import read_entities  # noqa: PLC0415

    cfg = load_config(config)
    rows = read_entities(entities)
    result = run_finalize(review_docx.read_bytes(), rows, cfg)
    for line in result.report.summary_lines():
        typer.echo(line)
    if show_values and result.report.leaks:
        ids = {h.entity for h in result.report.leaks}
        for row in rows:
            if (row.id or "neu") in ids:
                typer.echo(f"  {row.id}: {' | '.join(row.forms)}")
    if result.data is None:
        typer.secho("Leak-Check fehlgeschlagen: keine Ausgabe geschrieben.", fg="red", err=True)
        raise typer.Exit(code=1)
    out.write_bytes(result.data)
    typer.echo(f"Bereinigte Fassung: {out}")
    if keep_entities:
        typer.secho("WARNUNG: --keep-entities: Zuordnungsdatei bleibt bestehen (nur für Evaluation!).",
                    fg="yellow", err=True)
    else:
        entities.unlink()
        typer.echo(f"Zuordnungsdatei gelöscht: {entities}")
    typer.echo("Hinweis: Die Prüfkopie enthält weiterhin Originalwerte; gemäss Vorgaben entsorgen.")


@app.command("setup-models")
def setup_models(
    config: Optional[Path] = typer.Option(None, "--config", exists=True),
    allow_license: bool = typer.Option(False, "--allow-license", help="Accept a licence outside the allow-list"),
) -> None:
    """Download NER weights into ./models (the only command that uses the network)."""
    cfg = load_config(config)
    from .detect.ner import model_dir  # noqa: PLC0415

    try:
        from huggingface_hub import model_info, snapshot_download  # noqa: PLC0415
    except ImportError:
        typer.secho("huggingface_hub fehlt: pip install .[ner]", fg="red", err=True)
        raise typer.Exit(code=1) from None
    allowed = {x.lower() for x in cfg.rules["ner"].get("allowed_licenses", [])}
    ok = False
    for backend in cfg.rules["ner"].get("backends", []):
        model_id = cfg.rules["ner"]["models"][backend]
        try:
            info = model_info(model_id)
        except Exception as exc:
            typer.secho(f"{model_id}: nicht gefunden/erreichbar ({type(exc).__name__})", fg="yellow", err=True)
            continue
        lic = _model_license(info)
        typer.echo(f"{model_id}: Lizenz {lic}")
        if str(lic).lower() not in allowed and not allow_license:
            typer.secho(f"  übersprungen: Lizenz nicht in allowed_licenses ({', '.join(sorted(allowed))})",
                        fg="yellow", err=True)
            continue
        target = model_dir(cfg, backend)
        target.mkdir(parents=True, exist_ok=True)
        snapshot_download(model_id, local_dir=str(target))
        typer.echo(f"  gespeichert in {target}")
        ok = True
    if not ok:
        raise typer.Exit(code=1)


def _model_license(info) -> str:
    card = getattr(info, "card_data", None) or getattr(info, "cardData", None)
    lic = None
    if card is not None:
        data = card.to_dict() if hasattr(card, "to_dict") else (card if isinstance(card, dict) else {})
        lic = data.get("license")
    if not lic:
        lic = next((t.split(":", 1)[1] for t in (getattr(info, "tags", None) or []) if t.startswith("license:")),
                   None)
    return str(lic or "unknown")


@app.command()
def evaluate(
    pairs: Path = typer.Option(..., "--pairs", exists=True, file_okay=False, help="Directory with test documents"),
    out: Path = typer.Option(Path("eval_out"), "-o", "--out"),
    config: Optional[Path] = typer.Option(None, "--config", exists=True),
    no_llm: bool = typer.Option(False, "--no-llm"),
    no_ner: bool = typer.Option(False, "--no-ner"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Score against gold anonymised versions; review copies for unseen inputs."""
    _setup_logging(verbose)
    _offline()
    from .evaluate.compare import run_evaluation  # noqa: PLC0415

    cfg = load_config(config)
    llm = _llm_client(cfg, no_llm)
    report_path = run_evaluation(pairs, out, cfg, use_ner=not no_ner, llm=llm)
    typer.echo(f"Bericht: {report_path}")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
