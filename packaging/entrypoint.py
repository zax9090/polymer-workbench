"""PyInstaller entry: workers dispatch before importing GUI modules."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from workbench.__main__ import main
    raise SystemExit(main())
