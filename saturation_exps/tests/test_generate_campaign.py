# saturation_exps/tests/test_generate_campaign.py
import sys
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
from generate_campaign import parse_args


def test_parse_args_with_experiments():
    """Test parsing --experiments flag with comma-separated list."""
    args = parse_args(["--experiments", "exp1,exp3,exp5"])
    assert args.experiments == ["exp1", "exp3", "exp5"]


def test_parse_args_missing_experiments():
    """Test that missing --experiments raises error."""
    try:
        parse_args([])
        assert False, "Should have raised SystemExit"
    except SystemExit:
        pass  # Expected
