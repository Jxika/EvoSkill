from src.cli.main import cli

if __name__ == '__main__':
    import sys
    sys.argv = ['evoskill', 'run', '--continue']
    cli()