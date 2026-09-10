"""Filename date inference and its use when a call has no ICM record."""

from datetime import datetime

from backend.call_dates import filename_date_fields, infer_call_datetime
from backend.icm_parser import CallMeta
from backend.models import Job
from backend.pipeline import _init_calls


def test_gtl_epoch_filename_yields_local_date_and_time():
    epoch = 1647369886
    expected = datetime.fromtimestamp(epoch)
    assert infer_call_datetime(f"{epoch}_5000_12_161_857.wav") == (
        expected.strftime("%Y-%m-%d"),
        expected.strftime("%H:%M"),
    )


def test_stamped_filename_yields_date_and_time():
    assert infer_call_datetime("20260313_123302_9095550226.wav") == ("2026-03-13", "12:33")
    assert infer_call_datetime("call-20260313.mp3") == ("2026-03-13", "")
    assert infer_call_datetime("/batch/20260313T0905_x.wav") == ("2026-03-13", "09:05")


def test_unrecognized_or_invalid_names_yield_nothing():
    assert infer_call_datetime("call.wav") is None
    assert infer_call_datetime("20261340_000000.wav") is None      # month 13
    assert infer_call_datetime("0000000001_pin.wav") is None       # epoch far outside the window
    assert infer_call_datetime("5000_12_161_857.wav") is None      # no leading epoch
    assert filename_date_fields("call.wav") == {}


def test_filename_date_fields_shape():
    assert filename_date_fields("20260313_123302_x.wav") == {
        "call_date": "2026-03-13",
        "call_time": "12:33",
        "call_datetime_str": "2026-03-13 12:33",
    }


def _job() -> Job:
    return Job(id="j", case_name="Case", input_folder="/in", summary_prompt="p", created_at="2026-01-01")


def test_init_calls_prefers_icm_and_falls_back_to_the_filename():
    icm = CallMeta(
        inmate_name="A B", inmate_pin="1", outside_number="9095550144", outside_number_fmt="(909) 555-0144",
        call_date="2026-04-01", call_time="10:00", call_datetime_str="2026-04-01 10:00",
        facility="", call_outcome="", call_type="", xml_duration_seconds=60, notes="",
    )
    job = _job()
    _init_calls(job, ["/in/20260313_123302_a.wav", "/in/1647369886_5000_b.wav", "/in/plain.wav"],
                {"20260313_123302_a.wav": icm})

    by_name = {c.filename: c for c in job.calls}
    assert by_name["20260313_123302_a.wav"].call_date == "2026-04-01"       # ICM wins over the name
    assert by_name["20260313_123302_a.wav"].outside_number == "9095550144"
    inferred = by_name["1647369886_5000_b.wav"]
    assert inferred.call_date == datetime.fromtimestamp(1647369886).strftime("%Y-%m-%d")
    assert inferred.outside_number is None                                # nothing else is guessed
    assert by_name["plain.wav"].call_date is None
