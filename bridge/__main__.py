from bridge.main import main  # absolute — PyInstaller strips package context

if __name__ == "__main__":
    raise SystemExit(main())
