import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { IdentityService } from '../services/identity.service';

export const accountSelectedGuard: CanActivateFn = () => {
  const identityService = inject(IdentityService);
  const router = inject(Router);
  
  if (identityService.identity()) {
    return true;
  }
  
  router.navigate(['/select-account']);
  return false;
};
