# toyrepo

The toy repo for rework's vertical slice: `add()` has a planted bug (it
subtracts), `mul()` builds on `add()` and passes anyway. A fix mission is
`make add(2, 3) == 5`; the audit checks that `mul` still works after the fix.
