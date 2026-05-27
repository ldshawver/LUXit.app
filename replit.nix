{pkgs}: {
  deps = [
    pkgs.systemdUkify
    pkgs.libpq
    pkgs.postgresql
    pkgs.coreutils
  ];
}
