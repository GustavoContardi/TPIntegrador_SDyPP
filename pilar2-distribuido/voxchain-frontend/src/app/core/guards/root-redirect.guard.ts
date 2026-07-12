import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { IdentityService } from '../services/identity.service';

export const rootRedirectGuard: CanActivateFn = () => {
  const identityService = inject(IdentityService);
  const router = inject(Router);
  
  if (identityService.identity()) {
    router.navigate(['/dashboard']);
  } else {
    router.navigate(['/identity']);
  }
  return false;
};
