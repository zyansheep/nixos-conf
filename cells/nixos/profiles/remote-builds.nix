{
  inputs,
  common,
}: {
  users.users.root = {
    openssh.authorizedKeys.keys = [ ];
  };
}
