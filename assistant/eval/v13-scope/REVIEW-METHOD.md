# Review method

The numeric targets and scoring procedure are defined in [the registered protocol](protocol.json).
For this study, independent GPT-6.2 Sol contexts replace the planned cross-provider review.
Each sees only the source and items assigned to it. Authors and static reviewers have no
access to retrieval results or the scope implementation.

Two separate contexts judge the same recorded answers and boundary labels using a fixed rubric.
Agreement is calculated before a third context judges the disputed items without earlier verdicts.
The resulting agreement measures consistency within one model family.
