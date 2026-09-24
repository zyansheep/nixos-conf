_: {pkgs, ...}: {
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
    withRuby = false;
    withPython3 = false;

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
    in
      with pkgs.vimPlugins; [
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
