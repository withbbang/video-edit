import sys
from pathlib import Path

# scripts/ 를 import 경로에 넣어 `import pipeline` 이 되게 한다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
