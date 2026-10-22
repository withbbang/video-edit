import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# scripts/ 를 import 경로에 넣어 `import pipeline` 이 되게 하고, 저장소 루트도 넣어 `import app` 이 되게 한다
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
