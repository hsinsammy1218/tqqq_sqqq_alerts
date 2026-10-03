# Agent upload status (temporary)

`weekday_paper_schedule.py` on main is correct (daytime DEFAULT_SLOTS + lunch skip).

`main.py` and `README.md` currently contain accidental `$file:` placeholders from a failed MCP symbolic-path expand.
Target local blobs:

- README.md → `9d2ebf100e1971a9f6b5420c88a883597b38b12a`
- main.py → `b362abc572af1a55389c8102412bde96e34e7fa1`

Assemble staged chunks with:

```bash
python scripts/assemble_agent_upload.py
```

after all `.agent_upload/*.part*` files are present, then commit the two targets.
