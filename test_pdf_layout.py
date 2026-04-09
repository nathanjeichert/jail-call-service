"""Quick test: generate a sample transcript PDF to verify the new layout."""
import sys
sys.path.insert(0, ".")

from backend.models import TranscriptTurn
from backend.transcript_formatting import create_pdf

turns = [
    TranscriptTurn(speaker="Inmate", timestamp="[00:01]",
        text="Are you sleeping? You sleeping already?"),
    TranscriptTurn(speaker="Outside Party", timestamp="[00:04]",
        text="Well. Huh?"),
    TranscriptTurn(speaker="Inmate", timestamp="[00:06]",
        text="You sleeping yet?"),
    TranscriptTurn(speaker="Outside Party", timestamp="[00:08]",
        text="Um, and it kinda goes off, so I have to I find like ten minutes left at the movie"),
    TranscriptTurn(speaker="Inmate", timestamp="[00:15]",
        text="Oh, like you always do. Is it like you always do? Do you learn I miss you so much. I miss you so much. I desperately dude I said like desperately, man, I just hate being so far away, man."),
    TranscriptTurn(speaker="Outside Party", timestamp="[00:38]",
        text="We gotta do positive stuff."),
    TranscriptTurn(speaker="Inmate", timestamp="[00:41]",
        text="I know, I know. But listen, I talked to my lawyer today and he said some interesting things about the case, you know what I mean? He was telling me about the discovery documents and how they got some new information from the witnesses."),
    TranscriptTurn(speaker="Outside Party", timestamp="[01:02]",
        text="Oh really? What did he say exactly? Because I was just talking to your mom about it yesterday and she was really worried about the whole situation."),
    TranscriptTurn(speaker="Inmate", timestamp="[01:15]",
        text="Yeah so basically he said that the timeline doesn't match up with what they originally said. And they have to turn over all the evidence by next month."),
    TranscriptTurn(speaker="Outside Party", timestamp="[01:28]",
        text="That's good news right?"),
    TranscriptTurn(speaker="Inmate", timestamp="[01:30]",
        text="Yeah it's real good. Real good. I'm feeling optimistic for the first time in a while honestly."),
    TranscriptTurn(speaker="Outside Party", timestamp="[01:38]",
        text="Good. I put some money on your books today."),
    TranscriptTurn(speaker="Inmate", timestamp="[01:42]",
        text="Thank you baby. I appreciate that. Did you talk to my sister?"),
    TranscriptTurn(speaker="Outside Party", timestamp="[01:48]",
        text="Yeah she came by the house on Saturday and brought the kids. They were asking about you. Little Marcus drew you a picture, it was so cute."),
    TranscriptTurn(speaker="Inmate", timestamp="[02:00]",
        text="Man, that kills me. Tell him I love him. Tell all of them I love them. I think about them every single day in here."),
    TranscriptTurn(speaker="Outside Party", timestamp="[02:12]",
        text="I will. They know. We all know. Just stay strong and keep your head up in there, OK? Don't get into any trouble."),
    TranscriptTurn(speaker="Inmate", timestamp="[02:22]",
        text="I won't. I've been staying to myself mostly. Reading a lot. They got some new books in the library."),
]

title_data = {
    "CASE_NAME": "State v. Johnson",
    "INMATE_NAME": "Marcus Johnson",
    "CALL_DATETIME": "03/15/2026 02:34 PM",
    "FACILITY": "Unit 4B",
    "FILE_DURATION": "2:30",
    "OUTSIDE_NUMBER_FMT": "(555) 867-5309",
    "CALL_OUTCOME": "Completed",
    "FILE_NAME": "call_0001.wav",
    "AUDIO_FILENAME": "call_0001.mp3",
}

summary = """RELEVANCE: MEDIUM

KEY FINDINGS:
- Inmate discusses lawyer consultation and case discovery timeline
- Mentions witnesses and new evidence expected next month
- Family updates and commissary discussion

SPEAKERS & RELATIONSHIP:
Marcus Johnson (inmate) speaking with romantic partner (outside party). Familiar, affectionate tone throughout.

CALL SUMMARY:
Inmate begins with casual conversation before discussing a recent meeting with his attorney. The lawyer reported inconsistencies in the prosecution's timeline and an upcoming discovery deadline. The outside party relays family news including a visit from the inmate's sister and her children. The call ends with mutual expressions of support and the inmate describing his daily routine in custody.
"""

pdf_bytes = create_pdf(title_data, turns, summary=summary, audio_duration=150.0)
with open("test_layout_output.pdf", "wb") as f:
    f.write(pdf_bytes)
print(f"Written {len(pdf_bytes):,} bytes to test_layout_output.pdf")
