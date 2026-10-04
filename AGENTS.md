# Mobile UI workflow

- Make mobile UI changes in the `mobile-ui` worktree.
- After each mobile UI change, validate the UI, build the Android APK, and install and launch it on both connected phones (Huawei LIO-AL00 and Pixel 10a). The user has authorized this as the default workflow.
- Preserve device settings and credentials when updating. Do not install mobile UI updates on the tablet unless requested.
- If a phone is unavailable or installation fails, report which device was not updated.
