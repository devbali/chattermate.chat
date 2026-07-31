"""
Concolic testing support — target and symbolic decorators.

CallInterceptor is a singleton — all `@target` decorators and the
`.run()` call share the same instance automatically.

Usage in tests:
    from app.concolic import interceptor, target, symbolic_func
    dump = interceptor.run(entrypoint, {"arg": sym_value}, label="run1")

Usage in source code:
    from app.concolic import target, symbolic_func

    @target
    def my_function(x: int) -> bool:
        ...

No-op fallback: when py_runtime is not installed, these decorators
pass through to the original function unchanged.
"""

try:
    from py_runtime import CallInterceptor, symbolic_func as _py_symbolic_func

    # CallInterceptor is a singleton — __new__ returns the same instance
    interceptor = CallInterceptor()
    target = interceptor.target
    symbolic_func = _py_symbolic_func

except ImportError:
    interceptor = None

    def target(func):
        return func

    def symbolic_func(func=None, *, name=None):
        def decorator(f):
            return f
        if func is not None:
            return decorator(func)
        return decorator