# End-to-end tests

They drive the real application, built into the image, with a real browser (Playwright). Nothing
is mocked: a throwaway Armadok starts empty, the tests go through the first-run set-up, sign in,
and use the interface the way a person does.

```sh
./e2e/run.sh          # builds the image, starts a throwaway instance, runs the tests, cleans up
```

Needs Docker. The instance has no worker and no Docker access, so only what works without them
is covered (set-up, sign-in, machines, appearance, search, the converter, the phone layout).
