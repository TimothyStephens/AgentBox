"""OMP and Pi get the profile's AGENTS.md after the sandbox notes, as Claude gets the profile's CLAUDE.md (19 Nov:
both adapters wrote the notes alone, so the shared working rules in the profile reached no OMP or Pi session)."""
import os

from tests.unit.helpers import read_file
from agentbox import session
from tests.unit.test_omp import OmpSessionBase

RULES = "# Working rules\n\n- profile-rule-marker-7c1\n"


class ProfileInstructions(OmpSessionBase):
    def make_pi(self, preset="python", **extra):
        self.project("p1", preset=preset, **extra)
        s = session.make_session(self.settings_now(), "pi", session.load_project(self.settings_now(), "p1"),
                                 warn=lambda m: None)
        s.prepare(do_backup=False)
        self.addCleanup(s.teardown)
        return s

    def write_profile(self, agent):
        d = os.path.join(self.cfg, agent, "profile")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "AGENTS.md"), "w") as f:
            f.write(RULES)

    def make_omp(self, **extra):
        return self.make(**extra)

    def agents_md(self, s, agent):
        return read_file(os.path.join(s.settings.session_run_dir(s.name), f"{agent}-AGENTS.md"))

    def test_the_profile_agents_md_follows_the_notes(self):
        for agent, make in (("omp", self.make_omp), ("pi", self.make_pi)):
            with self.subTest(agent=agent):
                self.write_profile(agent)
                text = self.agents_md(make(), agent)
                self.assertIn("profile-rule-marker-7c1", text)
                self.assertLess(text.index("agentbox sandbox"), text.index("profile-rule-marker-7c1"),
                                "the notes lead, as in Claude's CLAUDE.md")

    def test_no_profile_means_notes_only(self):
        for agent, make in (("omp", self.make_omp), ("pi", self.make_pi)):
            with self.subTest(agent=agent):
                self.write_profile(agent)
                text = self.agents_md(make(profile="off"), agent)
                self.assertNotIn("profile-rule-marker-7c1", text)
                self.assertIn("agentbox sandbox", text)
