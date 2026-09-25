from setuptools import setup

setup(name='Weird.TV',
      version='2.0',
      description='Creepy funny weird tv channel',
      author='Polish',
      author_email='polish1987@gmail.com',
      url='https://github.com/p0lish/weird.tv',
      py_modules=['app', 'archiver', 'channels', 'channer', 'db', 'wsgi'],
      install_requires=['Flask>=3.0', 'requests>=2.32', 'gunicorn>=23'],
      )
