from src.cli.main import cli
import os
from dotenv import load_dotenv

load_dotenv()
if __name__ == '__main__':
    import sys
    print(bool(os.environ.get("DEEPSEEK_API_KEY")))  
    sys.argv = ['evoskill', 'run', '--continue']
    cli()