from pathlib import Path

p = Path("src/rework/cli/__init__.py")
s = p.read_text()

anchor = '''project_app = typer.Typer(no_args_is_help=True, add_completion=False, help="Projects (ADR 0005).")
app.add_typer(project_app, name="project")
'''
assert anchor in s
s = s.replace(
    anchor,
    anchor
    + '''
mission_app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Mission owner actions (retry-pr)."
)
app.add_typer(mission_app, name="mission")
''',
    1,
)

old = '''    raise typer.Exit(1)


@app.command()
def complaints(
'''
new = '''    raise typer.Exit(1)


# the same command under the `mission` group the docs use (`rework mission
# retry-pr ID`); the top-level spelling stays, like every other mission command
mission_app.command("retry-pr")(retry_pr_cmd)


@app.command()
def complaints(
'''
assert old in s
s = s.replace(old, new, 1)
p.write_text(s)
print("ok")
