---
status: accepted
---

# Build a framework-agnostic testing library

The product will be a standalone Python Testing Library, with no dependency on or privileged integration for pytest or another Host Test Framework. It will own Observation capture, Span selection, Expectations, graph matching, and failure diagnostics, but not test discovery, fixtures, suite execution, or a runner CLI. This supersedes ADR 0001's proposed thin pytest adapter: users may call the library from pytest, `unittest`, Behave, custom harnesses, or ordinary Python without the product treating any of them as its primary frontend.
