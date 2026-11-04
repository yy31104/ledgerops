# Review fix04: reproducible demo/lab subprocess tests

Five real subprocess smoke tests passed with parent uv overrides absent. Each child selects the active pytest interpreter environment, including in the deliberately offline tests. The documented entry remains `make bootstrap` then `make test`. The tests retain real Docker-unavailable diagnostics and resource cleanup assertions. The containing commit records this fix; see the final integrated evidence for the complete regression.
