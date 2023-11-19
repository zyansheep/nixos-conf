{
  inputs,
  cell,
}: {
  secrets = _: {
    imports = [
      ./_common.nix
    ];
  };
}
