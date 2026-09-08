#!/usr/bin/env python3
"""Entry point — run with: python run.py"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        # The session cookie sets Secure from the request scheme. Without
        # this, a TLS terminator in front of the app still reports "http"
        # to it, so Secure would never be set in exactly the deployment
        # that needs it. Only headers from the trusted hop are honoured.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
