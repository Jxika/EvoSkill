"""Local debugger entry — equivalent to ``evoskill run`` with optional flags."""

from dotenv import load_dotenv

load_dotenv()

from src.cli.main import cli

if __name__ == "__main__":
    import sys

    # Fresh run (drop --continue once you want to resume an existing frontier):
    sys.argv = ["evoskill", "run"]
    cli()
