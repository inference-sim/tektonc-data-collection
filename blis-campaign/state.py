"""Campaign state persistence."""
import json
import time
from pathlib import Path


VALID_TRANSITIONS = {
    "pending": {"deploying", "skipped"},
    "deploying": {"running", "retrying", "failed"},
    "running": {"downloading", "retrying", "failed"},
    "downloading": {"completed", "download_failed"},
    "retrying": {"deploying"},
    "failed": {"deploying"},  # for --only re-runs
    "download_failed": set(),
    "completed": set(),
    "skipped": set(),
}


class CampaignState:
    def __init__(self, campaign_dir):
        self.path = Path(campaign_dir) / "campaign-state.json"
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"experiments": {}, "started_at": None}

    def save(self):
        """Atomic write to prevent corruption on crash."""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.rename(self.path)

    def get(self, exp_id):
        """Get experiment state. Returns default pending state if not found."""
        return self.data["experiments"].get(
            str(exp_id), {"status": "pending", "attempts": 0}
        )

    def update(self, exp_id, **fields):
        """Update experiment state fields and save."""
        entry = self.get(exp_id)
        entry.update(fields)
        self.data["experiments"][str(exp_id)] = entry
        self.save()

    def set_status(self, exp_id, status, **extra):
        """Set experiment status with optional extra fields."""
        self.update(exp_id, status=status, updated_at=iso_now(), **extra)

    def mark_started(self):
        """Mark campaign as started."""
        if not self.data["started_at"]:
            self.data["started_at"] = iso_now()
            self.save()


def iso_now():
    """Return ISO 8601 timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def print_status(args):
    """Print campaign progress table (implements 'blis-campaign status')."""
    state = CampaignState(args.campaign)
    experiments = state.data.get("experiments", {})

    if not experiments:
        print("No experiments tracked yet. Run 'blis-campaign generate' first.")
        return 0

    # Count by status
    counts = {}
    for entry in experiments.values():
        s = entry.get("status", "pending")
        counts[s] = counts.get(s, 0) + 1

    total = len(experiments)
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    running = counts.get("running", 0)
    pending = counts.get("pending", 0)

    print(f"\nCampaign Status ({total} experiments)")
    print("=" * 50)

    status_order = [
        "completed", "running", "deploying", "downloading",
        "pending", "retrying", "failed", "download_failed", "skipped",
    ]
    for s in status_order:
        c = counts.get(s, 0)
        if c > 0:
            bar = "#" * c
            print(f"  {s:20s} {c:3d}  {bar}")

    print(f"\n  Progress: {completed}/{total} completed")

    # List failed experiments with details
    failed_exps = [
        (eid, e) for eid, e in experiments.items()
        if e.get("status") in ("failed", "download_failed")
    ]
    if failed_exps:
        print(f"\nFailed experiments:")
        for eid, e in sorted(failed_exps, key=lambda x: x[0]):
            reason = e.get("last_failure", "unknown")
            attempts = e.get("attempts", 0)
            print(f"  #{eid}: {reason} ({attempts} attempts)")

    # List running experiments with current task
    running_exps = [
        (eid, e) for eid, e in experiments.items()
        if e.get("status") in ("running", "deploying", "downloading")
    ]
    if running_exps:
        print(f"\nActive experiments:")
        for eid, e in sorted(running_exps, key=lambda x: x[0]):
            pr = e.get("pipeline_run", "?")
            started = e.get("started_at", "?")
            print(f"  #{eid}: {pr} (started {started})")

    if state.data.get("started_at"):
        print(f"\nCampaign started: {state.data['started_at']}")

    return 0
