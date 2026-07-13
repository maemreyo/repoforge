import shutil


class SystemExecutableLocator:
    def which(self, executable: str, *, path: str | None = None) -> str | None:
        return shutil.which(executable, path=path)
