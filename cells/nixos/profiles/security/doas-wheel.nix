{
  inputs,
  common,
}: _: {
  security.doas.extraRules = [
    {
      users = ["zyansheep"];
      keepEnv = true;
      persist = true;
    }
  ];
}
