import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// React Testing Library does not unmount between tests on its own outside its own runner
// integration, and a left-over tree makes the next test's queries ambiguous rather than failing
// outright — which is the confusing kind of failure.
afterEach(cleanup);
