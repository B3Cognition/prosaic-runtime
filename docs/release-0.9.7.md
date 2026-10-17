# Runtime 0.9.7

Adds trusted native-request token admission to shared execution budgets and
preserves captured usage and breach evidence when recorder completion fails.
The provider-neutral counter contract admits the serialized request before HTTP;
unknown and in-flight usage remain reserved.

Adds immutable typed tool outcomes and an opt-in bounded tool recovery policy.
Admission failures may permit argument correction within existing limits.
Denied, completed controlled failures and uncertain effects stop automatic
recovery. Tool callbacks are never retried by this policy. Default behavior,
model envelopes and existing journal formats remain compatible.

Release qualification uses the exact source commit and immutable wheel/sdist
bytes, including native, installed-package and PostgreSQL gates. The recorder
0.3.0 artifacts are compatibility witnesses and are not republished.
Lab retrieval memory remains experimental. No live-model efficacy claim follows
from this infrastructure release.
