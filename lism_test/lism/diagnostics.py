def print_identity(cam,LISM_ADDR):
 api=cam.api; dev=cam.dev
 print('Vendor',api.getvendorname(0))
 print('Product',api.getproductname(0))
 print('Serial',api.getserialnumber(0))
 print('FW',hex(api.getfwversion(0)))
 for n in ['getmcuversion','getctgversion','gethw1id','gethw1version','gethw2version','getinitstatus']:
  rc,val=getattr(dev,n)(LISM_ADDR)
  print(n,rc,val)
