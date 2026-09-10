# Supplemental frontend notices

Some upstream npm tarballs omit a standalone license file. These texts preserve
the upstream repository license (or README license section) for those packages.
`index.json` records each covered locked package version, the retrieved license
file's SHA-256, and its immutable upstream commit URL. These are supplemental
license-source commits, not claims about the exact code commit in an npm tarball.
The npm lock retains package source/version/integrity identities separately.

Assembly copies the package's own notices where present; otherwise it requires
a matching version and checksum here. Dependency upgrades must review/update
this index. Assembly never downloads license texts.
