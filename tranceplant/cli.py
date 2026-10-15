"""Typer commands and the wiring between the six stages. Owner: Bhoumik."""

import typer

app = typer.Typer(help="Find risky dependencies, swap them in a sandbox, test both sides, and ship the evidence.")


@app.callback()
def main():
    pass
