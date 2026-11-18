# Informational question routing

In round 4, the v1.2 router blocked four of 75 documentation questions before
retrieval. These were informational questions with prepositional or possessive
openings. This put the documentation refusal rate above 5% before the new
passage check ran.

From round 5, the candidate recognizes those question openings. It keeps the
existing directive, override and secret-request checks, the external-topic list,
the similarity cutoff of 0.5512 and the coverage cutoff of 0.597. The reference
uses the router from the original v1.2 release commit. Retrieval, corpus, targets,
sampling, intervals and single-attempt rules follow the original protocol.

Calibrate the passage cutoff on rows admitted by the candidate's earlier checks.
Use the original rule for choosing a cutoff from development scores. Report the
reference, candidate before passage support, and final candidate separately so
the effect of the routing change is visible.
