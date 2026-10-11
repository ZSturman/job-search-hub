"""Explicit startup for the optional capture endpoint."""

from job_search_hub.config import load_settings


def main() -> int:
    settings = load_settings()
    if not settings.capture.auth_token:
        print("Configure a capture token: jobhub setup --credential CAPTURE_AUTH_TOKEN")
        return 2
    try:
        import uvicorn
    except ImportError:
        print("Install notion-job-search-hub[capture] to start the capture endpoint.")
        return 2
    from job_search_hub.capture.server import create_app

    print(f"Capture: http://{settings.capture.bind_host}:{settings.capture.bind_port}/capture")
    uvicorn.run(
        create_app(settings),
        host=settings.capture.bind_host,
        port=settings.capture.bind_port,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
