import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from manager.model import Entry
from manager.workspaces import ai_homes, fixed_problem


class ArtifactHomesAreNotAiStorage(unittest.TestCase):
    def test_artifact_home_does_not_block_its_project_as_work_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            (project / "node_modules").mkdir(parents=True)
            record_home = Path(temp) / "codex-home"
            record_home.mkdir()
            artifact = Entry("Artifacts", project, "artifact:deps-node:x", "node_modules", str(project), 0,
                             [project / "node_modules"], "artifact",
                             {"tier": 2, "category": "deps-node", "method": "recycle"})
            record = Entry("Codex", record_home, "id", "t", str(project), 0, [])
            homes = ai_homes([artifact, record])
            self.assertIn(record_home, homes)
            self.assertNotIn(project, homes)
            self.assertNotEqual(fixed_problem(project, homes), "AI 앱 저장소와 겹침")


if __name__ == "__main__":
    unittest.main()
