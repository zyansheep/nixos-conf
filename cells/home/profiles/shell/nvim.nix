_: {
  config,
  pkgs,
  inputs,
  ...
}: let
  thm = config.themes.default;

  flake-plugins =
    # Simple, from the installation viewpoint, plugins. Names must match flake inputs.
    pkgs.lib.genAttrs [
      "jabs-nvim"
    ]
    (plugin-name:
      pkgs.vimUtils.buildVimPlugin {
        name = plugin-name;
        dontBuild = true;
        src = inputs.${plugin-name};
      });
in {
  home.packages = with pkgs; [
    # python39Packages.python-lsp-server
    # nodePackages.bash-language-server
    # nodePackages.vim-language-server
    # nodePackages.yaml-language-server
    # ccls
    # rnix-lsp
    # terraform-ls
  ];

  home.sessionVariables = {
    EDITOR = "nvim";
    GIT_EDITOR = "nvim";
    VISUAL = "nvim";
    DIFFPROG = "nvim -d";
    MANPAGER = "nvim +Man!";
    MANWIDTH = 999;
  };

  programs.neovim = {
    enable = true;
    package = pkgs.neovim-unwrapped;

    defaultEditor = true;
    viAlias = true;
    vimAlias = true;
    vimdiffAlias = true;

    plugins = let
      nvim-treesitter-with-plugins = pkgs.vimPlugins.nvim-treesitter.withPlugins (treesitter-plugins:
      with treesitter-plugins; [
        bash
        c
        cpp
        lua
        nix
        python
        zig
        rust
      ]);
    in with pkgs.vimPlugins; [
        nvim-lspconfig # language server config plugin
        nvim-treesitter-with-plugins # code highlighting
        plenary-nvim # common lua functions
        gruvbox
        # gruvbox-material
        mini-nvim # ???
        nerdtree # filetree viewer
        vimux # tmux integration
        direnv-vim # direnv integration
        telescope-nvim # fuzzy finder
        telescope-fzf-native-nvim
      ];

    extraConfig = builtins.readFile ./_files/init.vim;
  };
}
