"""C1-ctx-sem-r5 (oracle stage only): ONE changed collision-judge prompt. Nothing in production calls it.

JUDGE_PROMPT_R5 replaces sem-r2's JUDGE_PROMPT (brief_compress_sem2.JUDGE_PROMPT, untouched). Output format and
parse_judge are sem-r2's, unchanged: one word, REPLACE or ALSO, greedy, max 4 tokens.

Rule given to the judge: REPLACE when the NEWER statement names the same slot of the same subject with a new value
(change words such as moved / switched / changed / now / new count); ALSO only when both values can hold at once (two
different things, people or purposes). Few-shot examples are modelled on the dev sets r8 and r12 only (templates in
bench/ctx_compress_r8_eval.py and bench/ctx_compress_r12_eval.py, fresh code values); none is taken from v2, v3 or r15.
"""
import brief_compress_sem2 as sem2

JUDGE_PROMPT_R5 = (
    "A person said two things, the older one first. Decide whether the NEWER statement REPLACES the OLDER one.\n"
    "Answer REPLACE when the NEWER statement names the same slot of the same subject (the same thing, kept for the "
    "same purpose) with a new value, including when it says moved, switched, changed, now or new: the older value is "
    "then no longer true.\n"
    "Answer ALSO only when both values can hold at once, because they are two different things, people or purposes, "
    "even if the words are similar.\n\n"
    "OLDER: My shed key tag is Ibis-17.\nNEWER: Small change: my shed key tag is now Pike-82, they swapped the old one.\n"
    "Answer: REPLACE\n\n"
    "OLDER: My pump locker code is Ibis-41.\n"
    "NEWER: I moved the track pump to the back room, and its locker code reads Pike-93 now.\nAnswer: REPLACE\n\n"
    "OLDER: My kids are both on the family pass, the pass code is Cobalt-12.\n"
    "NEWER: My kids are now in the new holiday club downstairs, the pickup code is Onyx-34.\nAnswer: ALSO\n\n"
    "OLDER: My van is booked for the ferry on Friday, the booking ref is Cobalt-27.\n"
    "NEWER: My van is now booked in at the new garage for its service, the job card is Onyx-58.\nAnswer: ALSO\n\n"
    "OLDER: {old}\nNEWER: {new}\nAnswer with one word.")

parse_judge = sem2.parse_judge
