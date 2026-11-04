# Public history and recorded results

The public history was prepared on 2026-11-04 from a private development repository.
Real workstation paths and hostnames were replaced throughout text history; dependent
checksum manifests were repaired for the sanitized presentation files. Binary files,
financial fixture values and source-artifact digest fields were preserved.

Commit IDs changed. Authors, timestamps and commit messages were preserved, including
the original development pace. The original repository remains a private archive;
its raw objects were never uploaded into this new public repository.

[This map](history-rewrite-map.tsv) pairs original development revisions with their
sanitized public revisions. Existing reports, fixtures and source-provenance notes
retain the revision recorded when an experiment actually ran. Those labels are
historical identities, not claims that tests were rerun under a rewritten commit ID.
Use the map to find the corresponding sanitized source. Instructions in old records
for retrieving raw originals refer to the private archive, not the public object store.

The current source, tests, migrations, design reference package, teaching lab,
tutorial and fixtures were compared byte for byte before publication. Current changes
outside the README cleanup are workstation metadata and checksum dependencies only.
The unused Chinese overview and post drafts were removed from public history; the
post is maintained separately from the project.

Previously recorded source checksum lists describe their original verification
snapshot. They do not describe every later source tree. Presentation/checksum repairs
preserve that distinction rather than relabeling old results as new measurements.

The new public repository was scanned across reachable history before publication.
The original GitHub repository stays private to avoid exposing old cached or dangling
objects merely by changing its visibility. No credentials were discovered by the
privacy scan; synthetic credentials used by negative tests remain identifiable as
fixtures. This finite scan is not a general security certification.
