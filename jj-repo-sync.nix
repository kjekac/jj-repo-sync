{ lib, stdenvNoCC, python3, jujutsu, gitMinimal, openssh, makeWrapper
}:

assert lib.assertMsg (lib.versionAtLeast jujutsu.version "0.47.1")
  "jj-repo-sync requires JJ 0.47.1 or newer for native reconciliation";

stdenvNoCC.mkDerivation {
  pname = "jj-repo-sync";
  version = "1";
  dontUnpack = true;
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ python3.pkgs.pytest jujutsu gitMinimal openssh ];
  buildPhase = ''
    cp ${./jj-repo-sync.py} jj_repo_sync.py
    substituteInPlace jj_repo_sync.py \
      --subst-var-by jj ${jujutsu}/bin/jj \
      --subst-var-by git ${gitMinimal}/bin/git \
      --subst-var-by ssh ${openssh}/bin/ssh \
      --subst-var-by keygen ${openssh}/bin/ssh-keygen
  '';
  doCheck = true;
  passthru.supportsRepositoryAllowlist = true;
  passthru.nativeReconciliation = true;
  passthru.jujutsu = jujutsu;
  checkPhase = ''
    export HOME="$TMPDIR/home"
    mkdir -p "$HOME"
    cp ${./test-jj-repo-sync.py} test_jj_repo_sync.py
    pytest -q test_jj_repo_sync.py
  '';
  installPhase = ''
    mkdir -p "$out/lib" "$out/bin"
    cp jj_repo_sync.py "$out/lib/"
    makeWrapper ${python3}/bin/python3 "$out/bin/jj-repo-sync" \
      --add-flags "$out/lib/jj_repo_sync.py" \
      --prefix PATH : ${lib.makeBinPath [ gitMinimal openssh ]}
  '';
  meta.platforms = lib.platforms.unix;
}
