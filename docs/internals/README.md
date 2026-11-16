# Internal reference

Return to the [six operating guides](../README.md) for the reader path.

- Storage: [reservation index](reservation-index.md), [maintenance capacity](maintenance-capacity.md).
- Evidence: [export and verification](export-and-verify.md), [streaming export protocol](streaming-export-protocol.md), [streaming evidence](streaming-evidence.md), [evidence exchange](evidence-exchange.md).
- Operations: [backup and restore](backup-restore.md), [reversal](reversal.md), [release checks](release-readiness.md).
- Deployment: [local Kubernetes](kubernetes.md), [Azure preparation](azure.md).
- Decisions: [initialization](adr/0001-initialization.md), [ledger foundation](adr/0002-ledger-foundation.md), [purchase admission](adr/0003-purchase-admission.md), [contention and recovery](adr/0004-contention-and-recovery.md), [client histories](adr/0005-client-history-checking.md).

The assistant indexes an explicit operations corpus. This index and the project
homepage are navigation, not retrieval evidence. Corpus changes require a reviewed
development-label migration and a new baseline; moving a file cannot silently
relabel an old holdout result.
