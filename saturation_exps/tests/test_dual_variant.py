import sys
sys.path.insert(0, str(__file__).rsplit("/", 2)[0])
import pytest
from pathlib import Path
from generate_campaign import extract_variant_rates


def test_extract_variant_rates():
    """Test rate calculation for saturation and overloaded variants."""
    # From exp1 saturation results
    saturation_results = {
        "result": {
            "saturation_point_rps": 12.406919642857135,
            "final_precision_rps": 0.5639508928571413
        }
    }

    saturation_rate, overloaded_rate = extract_variant_rates(saturation_results)

    assert saturation_rate == 12.406919642857135
    # Overloaded = saturation + precision
    assert overloaded_rate == pytest.approx(12.970870535714277)
