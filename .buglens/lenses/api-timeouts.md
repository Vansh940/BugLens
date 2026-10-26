# API Resilience
exts: py,ts,js
category: performance
Flag any HTTP client call without a timeout, retries without backoff, and
unbounded fan-out (loops that await one request per item). Cite the call line.
